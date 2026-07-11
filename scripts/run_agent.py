#!/usr/bin/env python3
"""Role runner that only mutates GitHub via the API (visible audit trail).

Uses GITHUB_TOKEN (expected to be the role's GitHub App installation token).
Every role posts issue comments for start/finish. No silent local-only outcomes.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

from github_api import (
    GitHubError,
    add_labels,
    api,
    delete_label,
    post_issue_comment,
    split_repo,
)

HANDOFF_DIR = Path("trace")
BUILDER_HANDOFF = HANDOFF_DIR / "builder-handoff.txt"


def repo_from_env() -> str:
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        raise GitHubError("GITHUB_REPOSITORY is required")
    return repo


def issue_labels(repo: str, issue: int) -> set[str]:
    owner, name = split_repo(repo)
    data = api("GET", f"/repos/{owner}/{name}/issues/{issue}")
    return {label["name"] for label in data.get("labels") or []}


def replace_status(repo: str, issue: int, new_status: str) -> None:
    labels = issue_labels(repo, issue)
    for label in list(labels):
        if label.startswith("status:"):
            delete_label(repo, issue, label)
    add_labels(repo, issue, [new_status])


def ensure_label(repo: str, issue: int, label: str) -> None:
    add_labels(repo, issue, [label])


def remove_label(repo: str, issue: int, label: str) -> None:
    delete_label(repo, issue, label)


def get_issue(repo: str, issue: int) -> dict:
    owner, name = split_repo(repo)
    return api("GET", f"/repos/{owner}/{name}/issues/{issue}")


def slugify(text: str, limit: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (slug or "work")[:limit]


def linked_open_prs(repo: str, issue: int) -> list[dict]:
    owner, name = split_repo(repo)
    prs = api("GET", f"/repos/{owner}/{name}/pulls?state=open&per_page=100") or []
    needle = f"#{issue}"
    return [
        pr
        for pr in prs
        if needle in (pr.get("title") or "") or needle in (pr.get("body") or "")
    ]


def escalate(repo: str, issue: int, reason: str, assignee_hint: str | None = None) -> None:
    hint = f"\nSuggested assignee: @{assignee_hint}" if assignee_hint else ""
    post_issue_comment(
        repo,
        issue,
        f"@human-review\n\n{reason}{hint}\n\nAdding `status:blocked` and stopping.",
    )
    replace_status(repo, issue, "status:blocked")


def write_builder_handoff(mode: str) -> None:
    """Tell builder.yml how to advance labels: reviewer | done | blocked | waiting."""
    if mode not in {"reviewer", "done", "blocked", "waiting"}:
        raise GitHubError(f"invalid builder handoff mode: {mode!r}")
    HANDOFF_DIR.mkdir(parents=True, exist_ok=True)
    BUILDER_HANDOFF.write_text(mode + "\n", encoding="utf-8")


def is_verify_deploy_issue(title: str, body: str) -> bool:
    text = f"{title}\n{body}".lower()
    if not re.search(r"\bverify\b|\bsmoke\b", text):
        return False
    return bool(re.search(r"\brender\b|\bdeploy\b|onrender\.com|/health|/hello", text))


def close_linked_open_prs(repo: str, issue: int, reason: str) -> None:
    owner, name = split_repo(repo)
    for pr in linked_open_prs(repo, issue):
        number = int(pr["number"])
        api(
            "PATCH",
            f"/repos/{owner}/{name}/pulls/{number}",
            body={"state": "closed"},
        )
        post_issue_comment(
            repo,
            issue,
            f"### builder_cleanup\n- closed stub PR #{number}\n- reason: {reason}\n",
        )


def role_planner(repo: str, issue: int, brief: Path) -> None:
    data = get_issue(repo, issue)
    title = data.get("title") or f"issue-{issue}"
    body = data.get("body") or ""
    labels = {label["name"] for label in data.get("labels") or []}

    type_label = next((l for l in labels if l.startswith("type:")), None)
    if type_label is None:
        if re.search(r"\bdocs?\b", title + "\n" + body, re.I):
            type_label = "type:docs"
        elif re.search(r"\bbug\b|fix", title + "\n" + body, re.I):
            type_label = "type:bug"
        else:
            type_label = "type:feature"
        ensure_label(repo, issue, type_label)

    # Independent change areas: prefer explicit work-package headings.
    # Ignore acceptance checklists and meta sections so Planner does not
    # explode one feature into one child per checkbox.
    skip_sections = {
        "summary",
        "goal",
        "acceptance",
        "acceptance criteria",
        "out of scope",
        "notes",
        "recommended deploy",
        "recommended deploy (easiest)",
        "alternatives",
        "context",
        "background",
        "source",
        "about",
        "about (public linkedin facts)",
        "selected experience",
        "selected experience (brief)",
        "design",
        "constraints",
    }
    sections = [
        s.strip()
        for s in re.findall(r"(?m)^##\s+(.+)$", body)
        if s.strip().lower() not in skip_sections
    ]
    # Only treat checklist items as areas when there are no usable sections
    # and the list is small (real work breakdown, not acceptance criteria).
    tasks = re.findall(r"(?m)^\s*[-*] \[.\] (.+)$", body)
    if sections:
        areas = sections
    elif 1 < len(tasks) <= 4:
        areas = tasks
    else:
        areas = []

    max_children = 4
    agent_label = "agent:docs" if type_label == "type:docs" else "agent:builder"

    if len(areas) > 1:
        areas = areas[:max_children]
        children: list[int] = []
        owner, name = split_repo(repo)
        for area in areas:
            child = api(
                "POST",
                f"/repos/{owner}/{name}/issues",
                body={
                    "title": f"{title}: {area.strip()[:80]}",
                    "body": (
                        f"Child of #{issue} (one-commit unit).\n\n"
                        f"## Scope\n\n{area.strip()}\n\n"
                        f"Parent: #{issue}"
                    ),
                    "labels": [agent_label, type_label, "status:queued"],
                },
            )
            children.append(int(child["number"]))
            post_issue_comment(
                repo,
                child["number"],
                f"### planner_plan\nQueued as one-commit child of #{issue}.\n",
            )
        trace = Path(f"trace/planner-{issue}-children.txt")
        trace.parent.mkdir(parents=True, exist_ok=True)
        trace.write_text("\n".join(str(n) for n in children) + "\n", encoding="utf-8")
        plan = (
            f"### planner_plan\n"
            f"- mode: children\n"
            f"- type: `{type_label}`\n"
            f"- agent: `{agent_label}`\n"
            f"- children: {', '.join(f'#{n}' for n in children)}\n"
            f"- granularity: one commit per child\n"
            f"- brief: `{brief}`\n"
        )
        post_issue_comment(repo, issue, plan)
        for label in list(labels):
            if label.startswith("agent:"):
                remove_label(repo, issue, label)
        ensure_label(repo, issue, "agent:planner")
    else:
        for label in list(labels):
            if label.startswith("agent:"):
                remove_label(repo, issue, label)
        ensure_label(repo, issue, agent_label)
        plan = (
            f"### planner_plan\n"
            f"- mode: single\n"
            f"- type: `{type_label}`\n"
            f"- agent: `{agent_label}`\n"
            f"- granularity: one commit on this issue\n"
            f"- brief: `{brief}`\n"
        )
        post_issue_comment(repo, issue, plan)


def is_landing_issue(title: str, body: str) -> bool:
    text = f"{title}\n{body}".lower()
    return bool(
        re.search(r"\blanding\b|\babout page\b|saberistic\.com", text)
        and re.search(r"\bamirs?aber\b|\bsharifi\b|\blinkedin\b|\bwebsite\b|\bsite\b", text)
    )


def role_builder(repo: str, issue: int, brief: Path) -> None:
    data = get_issue(repo, issue)
    title = data.get("title") or f"issue-{issue}"
    body = data.get("body") or ""

    # Ops / verify issues: run smoke against production; no stub PR (avoids
    # reviewer↔builder loops on worklog-only PRs).
    if is_verify_deploy_issue(title, body):
        base_url = "https://saberistic.com"
        match = re.search(r"https://[a-z0-9.-]+\.onrender\.com", body, re.I)
        if match:
            base_url = match.group(0).rstrip("/")
        proc = subprocess.run(
            [sys.executable, "scripts/smoke_deploy.py", "--base-url", base_url],
            capture_output=True,
            text=True,
            check=False,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        post_issue_comment(
            repo,
            issue,
            (
                f"### builder_verify\n"
                f"- base_url: `{base_url}`\n"
                f"- exit: `{proc.returncode}`\n"
                f"- brief: `{brief}`\n\n"
                f"```\n{out.strip() or '(no output)'}\n```\n"
            ),
        )
        close_linked_open_prs(repo, issue, "verify-deploy does not need a code PR")
        if proc.returncode == 0:
            write_builder_handoff("done")
        else:
            escalate(
                repo,
                issue,
                f"Production smoke failed for `{base_url}` (exit {proc.returncode}).",
            )
            write_builder_handoff("blocked")
        return

    # Landing create-only: if site is missing, block. Landing *changes* use codegen.
    if is_landing_issue(title, body):
        site_index = Path("site/index.html")
        if not site_index.is_file():
            escalate(
                repo,
                issue,
                "Landing scaffold missing (`site/index.html`). Add the base site before Builder can iterate.",
            )
            write_builder_handoff("blocked")
            return

    # Screenshot / reviewer infra: already implemented in-repo — document + done PR path
    # without calling Models (often 403) or OpenAI UI prompts.
    # Landing/product issues must never take this shortcut (AC may mention screenshots).
    try:
        from codegen_models import is_agent_infra_issue
    except Exception:
        is_agent_infra_issue = lambda *_a, **_k: False  # noqa: E731

    if is_agent_infra_issue(title, body) and not is_landing_issue(title, body):
        owner, name = split_repo(repo)
        default = api("GET", f"/repos/{owner}/{name}").get("default_branch") or "main"
        branch = f"builder/{issue}-{slugify(title)}"
        ref = api("GET", f"/repos/{owner}/{name}/git/ref/heads/{default}")
        base_sha = ref["object"]["sha"]
        try:
            api(
                "POST",
                f"/repos/{owner}/{name}/git/refs",
                body={"ref": f"refs/heads/{branch}", "sha": base_sha},
            )
        except GitHubError as exc:
            if "Reference already exists" not in str(exc):
                raise
        # Ensure docs mention the two-phase screenshot flow.
        doc_path = Path("docs/SCREENSHOTS.md")
        if doc_path.is_file():
            content_b64 = __import__("base64").b64encode(doc_path.read_bytes()).decode()
            put_body = {
                "message": f"builder(#{issue}): sync screenshot docs",
                "content": content_b64,
                "branch": branch,
            }
            try:
                existing = api(
                    "GET",
                    f"/repos/{owner}/{name}/contents/{doc_path.as_posix()}?ref={branch}",
                )
                put_body["sha"] = existing["sha"]
            except GitHubError:
                pass
            api(
                "PUT",
                f"/repos/{owner}/{name}/contents/{doc_path.as_posix()}",
                body=put_body,
            )
        prs = linked_open_prs(repo, issue)
        if not prs:
            pr = api(
                "POST",
                f"/repos/{owner}/{name}/pulls",
                body={
                    "title": f"builder: {title} (#{issue})",
                    "head": branch,
                    "base": default,
                    "body": (
                        f"Closes #{issue}\n\n"
                        "Screenshot infra: pre-merge Reviewer captures + post-deploy "
                        "visual check (see docs/SCREENSHOTS.md).\n"
                    ),
                },
            )
            post_issue_comment(
                repo,
                issue,
                f"### builder_result\n- kind: `infra-screenshots`\n- pr: #{pr['number']}\n",
            )
        else:
            post_issue_comment(
                repo,
                issue,
                f"### builder_result\n- kind: `infra-screenshots`\n- existing_pr: #{prs[0]['number']}\n",
            )
        write_builder_handoff("reviewer")
        return

    # Product work: OpenAI primary; GitHub Models optional backup.
    try:
        from codegen_models import build_with_models

        result = build_with_models(
            repo,
            issue,
            title=title,
            body=body,
            brief=brief,
        )
        HANDOFF_DIR.mkdir(parents=True, exist_ok=True)
        (HANDOFF_DIR / "builder-model.txt").write_text(
            f"{result.get('provider')}:{result.get('model')}\n",
            encoding="utf-8",
        )
        write_builder_handoff("reviewer")
    except Exception as exc:
        escalate(
            repo,
            issue,
            (
                "Codegen failed (OpenAI / GitHub Models).\n\n"
                f"`{exc}`\n\n"
                "Required: ChatGPT via `OPENAI_API_KEY` (`CODEGEN_PROVIDER=openai`). "
                "Optional backup: GitHub Models (`MODELS_TOKEN`). Gemini is retired. "
                "See docs/MODELS.md. "
                "If `git/refs` returns 403 for the Builder App, grant the App "
                "`contents: write` on this repository."
            ),
        )
        write_builder_handoff("blocked")


def role_docs(repo: str, issue: int, brief: Path) -> None:
    data = get_issue(repo, issue)
    title = data.get("title") or f"issue-{issue}"
    body = data.get("body") or ""
    branch = f"docs/{issue}-{slugify(title)}"
    owner, name = split_repo(repo)
    default = api("GET", f"/repos/{owner}/{name}").get("default_branch") or "main"
    ref = api("GET", f"/repos/{owner}/{name}/git/ref/heads/{default}")
    base_sha = ref["object"]["sha"]
    try:
        api(
            "POST",
            f"/repos/{owner}/{name}/git/refs",
            body={"ref": f"refs/heads/{branch}", "sha": base_sha},
        )
    except GitHubError as exc:
        if "Reference already exists" not in str(exc):
            raise

    doc = textwrap.dedent(
        f"""\
        # Docs update for #{issue}

        {title}

        ## Requested

        {body.strip() or '_No issue body provided._'}

        ## Status

        Docs agent created this page as a visible PR artifact. Expand or
        replace with the authoritative documentation for the change.
        """
    )
    path = f"docs/agent-updates/{issue}.md"
    content_b64 = __import__("base64").b64encode(doc.encode()).decode()
    put_body = {
        "message": f"docs(#{issue}): add update stub",
        "content": content_b64,
        "branch": branch,
    }
    try:
        existing = api("GET", f"/repos/{owner}/{name}/contents/{path}?ref={branch}")
        put_body["sha"] = existing["sha"]
    except GitHubError:
        pass
    api("PUT", f"/repos/{owner}/{name}/contents/{path}", body=put_body)

    prs = linked_open_prs(repo, issue)
    if not prs:
        pr = api(
            "POST",
            f"/repos/{owner}/{name}/pulls",
            body={
                "title": f"docs: {title} (#{issue})",
                "head": branch,
                "base": default,
                "body": f"Closes #{issue}\n\nDocs agent brief: `{brief}`.\n",
            },
        )
        post_issue_comment(
            repo,
            issue,
            f"### docs_result\n- branch: `{branch}`\n- pr: #{pr['number']}\n",
        )
    else:
        post_issue_comment(
            repo,
            issue,
            f"### docs_result\n- branch: `{branch}`\n- existing_pr: #{prs[0]['number']}\n",
        )


def role_reviewer(repo: str, issue: int, brief: Path) -> None:
    prs = linked_open_prs(repo, issue)
    if not prs:
        escalate(repo, issue, "No open PR linked to this issue; cannot review.")
        raise SystemExit(1)
    pr = sorted(prs, key=lambda p: p.get("updated_at") or "", reverse=True)[0]
    owner, name = split_repo(repo)
    pr_number = pr["number"]

    # Hard fails from commit status / check runs when available
    sha = pr["head"]["sha"]
    status = api("GET", f"/repos/{owner}/{name}/commits/{sha}/status")
    combined = (status.get("state") or "pending").lower()
    hard_fail_reasons: list[str] = []
    if combined == "failure":
        hard_fail_reasons.append("combined commit status is failure")

    checks = api(
        "GET",
        f"/repos/{owner}/{name}/commits/{sha}/check-runs",
    )
    for run in checks.get("check_runs") or []:
        conclusion = (run.get("conclusion") or "").lower()
        name_l = (run.get("name") or "").lower()
        if conclusion in {"failure", "timed_out", "cancelled"}:
            hard_fail_reasons.append(f"check `{run.get('name')}` → {conclusion}")
        if "security" in name_l and conclusion == "failure":
            hard_fail_reasons.append(f"security check failed: {run.get('name')}")

    # Missing tests / stub-only / scaffold-sync heuristics
    files = api("GET", f"/repos/{owner}/{name}/pulls/{pr_number}/files") or []
    filenames = [f["filename"] for f in files]
    only_worklog = filenames and all(
        name.startswith(".agent/worklogs/") or name.endswith(".md")
        for name in filenames
    ) and any(name.startswith(".agent/worklogs/") for name in filenames)
    if only_worklog:
        hard_fail_reasons.append(
            "PR is builder worklog-only; no product code/tests to merge "
            "(terminal: true — do not requeue builder)"
        )

    commits = api("GET", f"/repos/{owner}/{name}/pulls/{pr_number}/commits") or []
    commit_msgs = [c.get("commit", {}).get("message", "") for c in commits]
    issue_data = api("GET", f"/repos/{owner}/{name}/issues/{issue}")
    issue_blob = f"{issue_data.get('title')}\n{issue_data.get('body')}".lower()
    infra_issue = bool(
        re.search(r"screenshot|headless|playwright|visual (check|evidence)", issue_blob)
    )
    if (
        commit_msgs
        and all(re.search(r"\bsync\b", m or "", re.I) for m in commit_msgs)
        and not infra_issue
    ):
        hard_fail_reasons.append(
            "PR commits are Builder scaffold sync only — does not implement the issue "
            "(terminal: true — do not approve)"
        )

    code_touched = any(
        not f["filename"].startswith(("docs/", "AGENTS/", ".agent/", "README"))
        and not f["filename"].endswith((".md", ".jsonl"))
        for f in files
    )
    tests_touched = any(
        "test" in f["filename"].lower() or f["filename"].startswith("tests/")
        for f in files
    )
    if code_touched and not tests_touched and any(
        f["filename"].endswith((".py", ".ts", ".js", ".go", ".rs")) for f in files
    ):
        hard_fail_reasons.append("behavior/code change without test file updates")

    # Pre-merge deploy screenshots (baseline before approve/merge)
    screenshot_note = ""
    try:
        from screenshot_deploy import (
            capture,
            comment_markdown,
            comment_on_issue_or_pr,
            resolve_base_url,
            upload_to_branch,
        )

        base_url = resolve_base_url(os.environ.get("DEPLOY_BASE_URL"))
        out_dir = Path("trace/screenshots")
        shots = capture(base_url, out_dir, phase="pre")
        branch = pr["head"]["ref"]
        urls = upload_to_branch(
            repo, branch, shots, f".agent/screenshots/pr-{pr_number}"
        )
        body_shots = comment_markdown("### reviewer_screenshots_pre", base_url, urls)
        comment_on_issue_or_pr(repo, pr_number, body_shots)
        comment_on_issue_or_pr(repo, issue, body_shots)
        screenshot_note = f"- screenshots_pre: {len(urls)} posted on PR + issue\n"
        pr = api("GET", f"/repos/{owner}/{name}/pulls/{pr_number}")
        sha = pr["head"]["sha"]
    except Exception as exc:
        screenshot_note = f"- screenshots_pre: failed (`{exc}`)\n"
        if os.environ.get("SCREENSHOTS_REQUIRED", "true").lower() in {"1", "true", "yes"}:
            hard_fail_reasons.append(f"required deploy screenshots failed: {exc}")

    # OpenAI / Models AI review (required for approve path).
    ai_block = ""
    try:
        from review_models import ai_review

        verdict = ai_review(repo, issue, pr_number)
        ai_block += (
            f"- ai_provider: `openai-or-models`\n"
            f"- ai_model: `{verdict.get('model')}`\n"
            f"- ai_decision: `{verdict.get('decision')}`\n"
            f"- ai_summary: {verdict.get('summary')}\n"
            "- ai_reasons:\n"
            + "\n".join(f"  - {r}" for r in (verdict.get("reasons") or []))
            + "\n"
        )
        if verdict.get("decision") != "approved":
            hard_fail_reasons.append(
                "AI reviewer rejected: "
                + "; ".join(verdict.get("reasons") or ["does not meet acceptance"])
            )
    except Exception as exc:
        hard_fail_reasons.append(f"AI reviewer unavailable: {exc}")
        ai_block += f"- ai_review: failed (`{exc}`)\n"

    # Acceptance criteria checklist with evidence (required before approve)
    acceptance_note = ""
    try:
        from acceptance import post_checklist, update_issue_checkboxes, verify_acceptance

        acceptance = verify_acceptance(repo, issue, pr_number, use_ai=True)
        checklist_comment = post_checklist(
            repo, issue, acceptance, role="reviewer"
        )
        acceptance_note = (
            f"- acceptance_all_done: `{str(bool(acceptance.get('all_done'))).lower()}`\n"
            f"- acceptance_checklist: {checklist_comment.get('html_url')}\n"
        )
        if acceptance.get("all_done"):
            try:
                update_issue_checkboxes(repo, issue, acceptance)
            except Exception as body_exc:
                acceptance_note += f"- acceptance_body_update: failed (`{body_exc}`)\n"
        else:
            hard_fail_reasons.append(
                "acceptance criteria incomplete — see acceptance_checklist comment "
                + (checklist_comment.get("html_url") or "")
            )
    except Exception as exc:
        acceptance_note = f"- acceptance_checklist: failed (`{exc}`)\n"
        hard_fail_reasons.append(f"acceptance checklist failed: {exc}")

    if hard_fail_reasons:
        event = "REQUEST_CHANGES"
        body = (
            "### reviewer_decision\n"
            f"- decision: `changes-requested`\n"
            f"- brief: `{brief}`\n"
            f"{screenshot_note}"
            f"{ai_block}"
            f"{acceptance_note}"
            "- hard_fails:\n"
            + "\n".join(f"  - {r}" for r in hard_fail_reasons)
            + "\n"
        )
    else:
        event = "APPROVE"
        body = (
            "### reviewer_decision\n"
            f"- decision: `approved`\n"
            f"- brief: `{brief}`\n"
            f"{screenshot_note}"
            f"{ai_block}"
            f"{acceptance_note}"
            "- judgment: AI + checks + acceptance checklist agree; nits non-blocking\n"
        )

    api(
        "POST",
        f"/repos/{owner}/{name}/pulls/{pr_number}/reviews",
        body={"commit_id": sha, "body": body, "event": event},
    )
    post_issue_comment(
        repo,
        issue,
        body + f"- pr: #{pr_number}\n",
    )


ROLES = {
    "planner": role_planner,
    "builder": role_builder,
    "reviewer": role_reviewer,
    "docs": role_docs,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", required=True, choices=sorted(ROLES))
    parser.add_argument("--issue", required=True, type=int)
    parser.add_argument("--brief", required=True, type=Path)
    args = parser.parse_args(argv)

    repo = repo_from_env()
    if not args.brief.is_file():
        print(f"FAIL: brief not found: {args.brief}", file=sys.stderr)
        return 1

    post_issue_comment(
        repo,
        args.issue,
        f"### agent_start\n- role: `{args.role}`\n- brief: `{args.brief}`\n",
    )
    try:
        ROLES[args.role](repo, args.issue, args.brief)
    except Exception as exc:
        post_issue_comment(
            repo,
            args.issue,
            f"### agent_failed\n- role: `{args.role}`\n- error: `{exc}`\n",
        )
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    post_issue_comment(
        repo,
        args.issue,
        f"### agent_finish\n- role: `{args.role}`\n- status: ok\n",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
