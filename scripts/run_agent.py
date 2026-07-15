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
from typing import Any

from dispatch_queue import replace_priority_label
from github_api import (
    GitHubError,
    add_labels,
    api,
    delete_label,
    post_issue_comment,
    split_repo,
)
from milestones import (
    ensure_open_milestone,
    issue_milestone_number,
    list_open_milestones,
    open_milestone_numbers,
    pick_current_milestone,
)
from pr_labels import apply_pr_mirror
from priority import (
    priority_labels_on_issue,
    resolve_priority_label,
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


def is_retryable_codegen_failure(exc: BaseException) -> bool:
    """True when Builder should re-enter ``status:queued`` instead of blocking.

    Learned from [#104](https://github.com/saberistic-team/agent-web/issues/104)
    (Cursor Bridge ``ReadTimeout``, ``retryable=True`` escalated to
    ``status:blocked``) and [#105](https://github.com/saberistic-team/agent-web/issues/105)
    (file-budget overrun → false human-review). Transient SDK/network failures and
    soft budget hits are operator-retriable by the dispatcher — not external
    blockers.
    """
    text = str(exc).lower()
    compact = text.replace(" ", "")
    if "retryable=true" in compact:
        return True
    # Bridge argv bug: token_urlsafe values starting with "-" → SDK reports
    # retryable=False, but a requeue (or the token patch) recovers.
    if "tool-callback-auth-token" in text or "bridge exited before discovery" in text:
        return True
    markers = (
        "readtimeout",
        "timed out",
        "bridge request timed out",
        "temporarily unavailable",
        "connection reset",
        "connection aborted",
        "connection error",
        "remoteprotocolerror",
        "server disconnected",
        "too many files",
        "changed too many files",
        "model proposed too many files",
    )
    return any(marker in text for marker in markers)


def write_builder_handoff(mode: str) -> None:
    """Tell builder.yml how to advance labels: reviewer | done | blocked | waiting."""
    if mode not in {"reviewer", "done", "blocked", "waiting"}:
        raise GitHubError(f"invalid builder handoff mode: {mode!r}")
    HANDOFF_DIR.mkdir(parents=True, exist_ok=True)
    BUILDER_HANDOFF.write_text(mode + "\n", encoding="utf-8")


def handoff_builder_when_mergeable(repo: str, issue: int) -> None:
    """Resolve conflicts if needed; hand off to Reviewer only when the PR is clean.

    Unresolved conflicts re-enter the priority queue (``waiting``) so Builder
    runs again — never send a dirty PR to Reviewer. Even mergeable/clean heads
    are smoke-imported so stale NameErrors cannot bounce Reviewer↔Builder.
    """
    from builder_conflicts import (
        linked_open_prs,
        linked_pr_conflict_status,
        maybe_resolve_pr_conflicts,
        refresh_pr,
        smoke_pr_head,
    )

    HANDOFF_DIR.mkdir(parents=True, exist_ok=True)
    _CONFLICT_OK = frozenset({"clean", "merged_clean", "resolved", "no_pr"})

    try:
        conflict = maybe_resolve_pr_conflicts(repo, issue)
        conflict_status = (conflict.get("status") or "").strip()
        (HANDOFF_DIR / "builder-conflict.txt").write_text(
            f"{conflict_status}\n",
            encoding="utf-8",
        )
        if conflict_status and conflict_status not in _CONFLICT_OK:
            post_issue_comment(
                repo,
                issue,
                (
                    "### builder_conflict_result\n"
                    f"- status: `{conflict_status}`\n"
                    f"- pr: #{conflict.get('pr')}\n"
                    "- note: conflict resolution did not finish cleanly; "
                    "re-entering `status:queued` (not handing off to Reviewer).\n"
                ),
            )
            write_builder_handoff("waiting")
            return
    except Exception as conflict_exc:
        post_issue_comment(
            repo,
            issue,
            (
                "### builder_conflict_result\n"
                f"- status: `failed`\n"
                f"- error: `{conflict_exc}`\n"
                "- note: codegen succeeded; unresolved merge conflicts — "
                "re-entering `status:queued` (do not open a second branch).\n"
            ),
        )
        (HANDOFF_DIR / "builder-conflict.txt").write_text("failed\n", encoding="utf-8")
        write_builder_handoff("waiting")
        return

    status = linked_pr_conflict_status(repo, issue)
    if status.get("status") == "dirty":
        post_issue_comment(
            repo,
            issue,
            (
                "### builder_conflict_result\n"
                f"- status: `still_dirty`\n"
                f"- pr: #{status.get('pr')}\n"
                f"- mergeable: `{status.get('mergeable')}`\n"
                f"- mergeable_state: `{status.get('mergeable_state')}`\n"
                "- note: not handing off to Reviewer; re-entering `status:queued` "
                "to keep resolving on the same PR head.\n"
            ),
        )
        (HANDOFF_DIR / "builder-conflict.txt").write_text(
            "still_dirty\n", encoding="utf-8"
        )
        write_builder_handoff("waiting")
        return

    # Always smoke the PR head — mergeable/clean heads can still NameError.
    prs = linked_open_prs(repo, issue)
    if prs:
        pr = refresh_pr(repo, int(prs[0]["number"]))
        try:
            smoke = smoke_pr_head(repo, pr, push_repair=True)
        except Exception as smoke_exc:
            post_issue_comment(
                repo,
                issue,
                (
                    "### builder_smoke_result\n"
                    f"- status: `failed`\n"
                    f"- pr: #{pr.get('number')}\n"
                    f"- error: `{smoke_exc}`\n"
                    "- note: re-entering `status:queued` (smoke clone/import failed).\n"
                ),
            )
            write_builder_handoff("waiting")
            return
        smoke_status = (smoke.get("status") or "").strip()
        if smoke_status == "smoke_failed":
            post_issue_comment(
                repo,
                issue,
                (
                    "### builder_smoke_result\n"
                    f"- status: `smoke_failed`\n"
                    f"- pr: #{smoke.get('pr')}\n"
                    f"- smoke_error: `{smoke.get('smoke_error')}`\n"
                    f"- repairs: `{smoke.get('repairs')}`\n"
                    "- note: not handing off to Reviewer; re-entering "
                    "`status:queued` until `from app.main import app` succeeds.\n"
                ),
            )
            write_builder_handoff("waiting")
            return
        if smoke_status == "smoke_repaired":
            post_issue_comment(
                repo,
                issue,
                (
                    "### builder_smoke_result\n"
                    f"- status: `smoke_repaired`\n"
                    f"- pr: #{smoke.get('pr')}\n"
                    f"- repairs: `{smoke.get('repairs')}`\n"
                    "- note: restored missing ``app.main`` wiring; proceeding to Reviewer.\n"
                ),
            )

    write_builder_handoff("reviewer")


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


# Narrative / meta H2s must never become Planner children (learned from #55).
PLANNER_SKIP_SECTIONS = {
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
    "current behavior",
    "current behavior (study notes)",
    "desired behavior",
    "implementation hints",
    "implementation notes",
    "user flow",
    "requirements",
    "routes",
    "environment variables",
    "local development",
    "database schema",
    "tests",
    "production",
    "production (render)",
    "parent context",
    "study notes",
    "what was wrong",
    "follow-up",
}


def extract_acceptance_section(body: str) -> str:
    """Return the ## Acceptance criteria section (heading + bullets), if any."""
    match = re.search(
        r"(?ims)^##\s+Acceptance criteria\s*\n(.*?)(?=^##\s+|\Z)",
        body or "",
    )
    if not match:
        return ""
    return f"## Acceptance criteria\n\n{match.group(1).strip()}\n"


def child_issue_body(parent_issue: int, area: str, parent_body: str) -> str:
    """Build a child issue body with scope + parent acceptance criteria."""
    acceptance = extract_acceptance_section(parent_body)
    if not acceptance:
        acceptance = (
            "## Acceptance criteria\n\n"
            f"- [ ] Implements scoped change from parent #{parent_issue}\n"
        )
    return (
        f"Child of #{parent_issue} (one-commit unit).\n\n"
        f"## Scope\n\n{area.strip()}\n\n"
        f"Parent: #{parent_issue}\n\n"
        f"{acceptance}"
    )


def plan_change_areas(body: str) -> list[str]:
    """Choose independent one-commit work areas from an issue body.

    Prefer an explicit ``## Work packages`` (or Change areas / Children) bullet
    list. Otherwise only treat remaining H2s as areas after skipping narrative
    sections — never invent children from study notes / desired-behavior prose.
    """
    text = body or ""
    work_pkg = re.search(
        r"(?ims)^##\s+(Work packages|Change areas|Children|Implementation units)\s*\n"
        r"(.*?)(?=^##\s+|\Z)",
        text,
    )
    if work_pkg:
        items = re.findall(
            r"(?m)^\s*[-*]\s+(?:\[[ xX]\]\s+)?(.+)$",
            work_pkg.group(2),
        )
        areas = [item.strip() for item in items if item.strip()]
        return areas[:4]

    sections = [
        s.strip()
        for s in re.findall(r"(?m)^##\s+(.+)$", text)
        if s.strip().lower() not in PLANNER_SKIP_SECTIONS
    ]
    # Ignore leftover prose headings that are not actionable work packages.
    actionable = [
        s
        for s in sections
        if re.match(
            r"(?i)^(add|implement|update|fix|remove|refactor|migrate|wire|docs?)\b",
            s,
        )
        or re.search(r"(?i)\b(api|ui|form|webhook|email|db|schema|test)\b", s)
    ]
    if len(actionable) > 1:
        return actionable[:4]

    # Never treat Acceptance criteria checkboxes as work packages (#55).
    return []


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

    priority_label = resolve_priority_label(title, body, labels)
    existing_priority = priority_labels_on_issue(labels)
    if not existing_priority:
        ensure_label(repo, issue, priority_label)
    elif len(existing_priority) != 1 or existing_priority[0] != priority_label:
        replace_priority_label(repo, issue, priority_label)

    areas = plan_change_areas(body)
    max_children = 4
    agent_label = "agent:docs" if type_label == "type:docs" else "agent:builder"

    open_milestones = list_open_milestones(repo)
    open_numbers = open_milestone_numbers(open_milestones)
    parent_milestone = issue_milestone_number(data)
    if parent_milestone is not None and parent_milestone in open_numbers:
        milestone_number = parent_milestone
        milestone_title = (data.get("milestone") or {}).get("title") or str(
            parent_milestone
        )
    else:
        current = pick_current_milestone(open_milestones)
        milestone_number = int(current["number"]) if current else None
        milestone_title = (current or {}).get("title") or "(none)"

    if len(areas) > 1:
        areas = areas[:max_children]
        children: list[int] = []
        owner, name = split_repo(repo)
        for area in areas:
            # Queue only: dispatcher applies agent:* by priority order.
            child_body: dict[str, Any] = {
                "title": f"{title}: {area.strip()[:80]}",
                "body": child_issue_body(issue, area, body),
                "labels": [
                    type_label,
                    priority_label,
                    "status:queued",
                ],
            }
            if milestone_number is not None:
                child_body["milestone"] = milestone_number
            child = api(
                "POST",
                f"/repos/{owner}/{name}/issues",
                body=child_body,
            )
            children.append(int(child["number"]))
            post_issue_comment(
                repo,
                child["number"],
                (
                    f"### planner_plan\n"
                    f"Queued as one-commit child of #{issue}.\n"
                    f"- type: `{type_label}`\n"
                    f"- priority: `{priority_label}`\n"
                    f"- milestone: `{milestone_title}`\n"
                    f"- intended_agent: `{agent_label}`\n"
                    "- awaiting: dispatcher (priority queue)\n"
                ),
            )
        trace = Path(f"trace/planner-{issue}-children.txt")
        trace.parent.mkdir(parents=True, exist_ok=True)
        trace.write_text("\n".join(str(n) for n in children) + "\n", encoding="utf-8")
        plan = (
            f"### planner_plan\n"
            f"- mode: children\n"
            f"- type: `{type_label}`\n"
            f"- priority: `{priority_label}`\n"
            f"- milestone: `{milestone_title}`\n"
            f"- intended_agent: `{agent_label}`\n"
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
        # Do not apply agent:builder/docs yet — dispatcher starts runs by priority.
        assigned = ensure_open_milestone(
            repo,
            issue,
            data,
            labels={*labels, priority_label},
            open_milestones=open_milestones,
        )
        if assigned and assigned.get("title"):
            milestone_title = str(assigned["title"])
        elif priority_label == "priority:critical":
            existing = (data.get("milestone") or {}).get("title")
            milestone_title = existing or "(critical; milestone optional)"
        for label in list(labels):
            if label.startswith("agent:"):
                remove_label(repo, issue, label)
        ensure_label(repo, issue, "agent:planner")
        plan = (
            f"### planner_plan\n"
            f"- mode: single\n"
            f"- type: `{type_label}`\n"
            f"- priority: `{priority_label}`\n"
            f"- milestone: `{milestone_title}`\n"
            f"- intended_agent: `{agent_label}`\n"
            f"- granularity: one commit on this issue\n"
            f"- awaiting: dispatcher after `status:queued`\n"
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
            pr_number = int(pr["number"])
            apply_pr_mirror(
                repo,
                issue,
                pr_number,
                default_review="review:needs-review",
            )
            post_issue_comment(
                repo,
                issue,
                f"### builder_result\n- kind: `infra-screenshots`\n- pr: #{pr_number}\n",
            )
        else:
            pr_number = int(prs[0]["number"])
            apply_pr_mirror(
                repo,
                issue,
                pr_number,
                default_review="review:needs-review",
            )
            post_issue_comment(
                repo,
                issue,
                f"### builder_result\n- kind: `infra-screenshots`\n- existing_pr: #{pr_number}\n",
            )
        handoff_builder_when_mergeable(repo, issue)
        return

    # Product work: Cursor SDK preferred; OpenAI / GitHub Models optional backups.
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
        # After codegen, merge base if dirty; hand off only when mergeable.
        handoff_builder_when_mergeable(repo, issue)
    except Exception as exc:
        detail = (
            "Codegen failed (Cursor SDK / OpenAI / GitHub Models).\n\n"
            f"`{exc}`\n\n"
            "Preferred: Cursor Agent SDK via `CURSOR_API_KEY` "
            "(`CODEGEN_PROVIDER=cursor`, `CURSOR_RUNTIME=local` by default). "
            "Optional: OpenAI (`OPENAI_API_KEY`) or GitHub Models (`MODELS_TOKEN`). "
            "See docs/MODELS.md. "
            "If `git/refs` returns 403 for the Builder App, grant the App "
            "`contents: write` on this repository."
        )
        if is_retryable_codegen_failure(exc):
            post_issue_comment(
                repo,
                issue,
                (
                    "### builder_codegen_retry\n"
                    "- result: `waiting`\n"
                    "- reason: transient / soft codegen failure (do not `status:blocked`)\n\n"
                    f"{detail}\n"
                ),
            )
            write_builder_handoff("waiting")
            return
        escalate(repo, issue, detail)
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
        pr_number = int(pr["number"])
        # Docs skips Reviewer; mirror type/priority only (no review:*).
        apply_pr_mirror(repo, issue, pr_number, default_review=None)
        post_issue_comment(
            repo,
            issue,
            f"### docs_result\n- branch: `{branch}`\n- pr: #{pr_number}\n",
        )
    else:
        pr_number = int(prs[0]["number"])
        apply_pr_mirror(repo, issue, pr_number, default_review=None)
        post_issue_comment(
            repo,
            issue,
            f"### docs_result\n- branch: `{branch}`\n- existing_pr: #{pr_number}\n",
        )


def role_reviewer(repo: str, issue: int, brief: Path) -> None:
    prs = linked_open_prs(repo, issue)
    if not prs:
        escalate(repo, issue, "No open PR linked to this issue; cannot review.")
        raise SystemExit(1)
    pr = sorted(prs, key=lambda p: p.get("updated_at") or "", reverse=True)[0]
    owner, name = split_repo(repo)
    pr_number = pr["number"]

    hard_fail_reasons: list[str] = []

    # Merge conflicts first — return to Builder; skip the rest of the budget.
    try:
        from builder_conflicts import (
            format_merge_conflict_hard_fail,
            linked_pr_conflict_status,
        )

        conflict_status = linked_pr_conflict_status(repo, issue)
        if conflict_status.get("status") == "dirty":
            hard_fail_reasons.append(format_merge_conflict_hard_fail(conflict_status))
        elif conflict_status.get("pr_payload"):
            pr = conflict_status["pr_payload"]
            pr_number = int(pr["number"])
    except Exception as conflict_exc:
        hard_fail_reasons.append(
            f"mergeability check failed: {conflict_exc} — "
            "treat as conflict and return to Builder"
        )

    if hard_fail_reasons:
        event = "REQUEST_CHANGES"
        body = (
            "### reviewer_decision\n"
            f"- decision: `changes-requested`\n"
            f"- brief: `{brief}`\n"
            "- hard_fails:\n"
            + "\n".join(f"  - {r}" for r in hard_fail_reasons)
            + "\n"
        )
        api(
            "POST",
            f"/repos/{owner}/{name}/pulls/{pr_number}/reviews",
            body={"commit_id": pr["head"]["sha"], "event": event, "body": body},
        )
        post_issue_comment(repo, issue, body)
        return

    # Hard fails from commit status / check runs when available
    sha = pr["head"]["sha"]
    status = api("GET", f"/repos/{owner}/{name}/commits/{sha}/status")
    combined = (status.get("state") or "pending").lower()
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

    # Service coverage gates: unit ≥90% / integration ≥70% of app/
    coverage_note = ""
    app_touched = any(
        f["filename"].startswith("app/") and f["filename"].endswith(".py")
        for f in files
    )
    for run in checks.get("check_runs") or []:
        name_l = (run.get("name") or "").lower()
        conclusion = (run.get("conclusion") or "").lower()
        if "coverage" in name_l and conclusion in {
            "failure",
            "timed_out",
            "cancelled",
        }:
            hard_fail_reasons.append(
                "service coverage check failed "
                "(unit ≥90% / integration ≥70% of app/ required)"
            )
    if app_touched:
        cov_root = (os.environ.get("COVERAGE_ROOT") or "").strip()
        cov_cmd = [sys.executable, "scripts/check_coverage.py"]
        if cov_root:
            cov_cmd.extend(["--root", cov_root])
        try:
            cov = subprocess.run(
                cov_cmd,
                capture_output=True,
                text=True,
                check=False,
            )
            cov_out = ((cov.stdout or "") + (cov.stderr or "")).strip()
            if cov.returncode != 0:
                hard_fail_reasons.append(
                    "service coverage below required thresholds "
                    "(unit ≥90% / integration ≥70% of app/): "
                    + (cov_out[-1200:] if cov_out else f"exit {cov.returncode}")
                )
                coverage_note = "- coverage: `failed` (see hard_fails)\n"
            else:
                coverage_note = (
                    "- coverage: `ok` (unit≥90% / integration≥70% of `app/`)\n"
                )
        except Exception as exc:
            hard_fail_reasons.append(f"service coverage check failed to run: {exc}")
            coverage_note = f"- coverage: failed (`{exc}`)\n"
    else:
        coverage_note = "- coverage: skipped (PR does not touch app/*.py)\n"

    # Pre-merge screenshots: PR-head only (incl. admin via ADMIN_PREVIEW_MODE).
    # saberistic.com shots are post-deploy only.
    screenshot_note = ""
    try:
        from screenshot_deploy import (
            comment_markdown_pre_dual,
            comment_on_issue_or_pr,
            capture_pre_dual,
            fetch_pr_changed_paths,
            format_admin_nav_hard_fail,
            format_empty_data_hard_fail,
            format_overflow_hard_fail,
            resolve_screenshot_targets,
            upload_to_branch,
        )

        out_dir = Path("trace/screenshots")
        changed = fetch_pr_changed_paths(repo, pr_number)
        targets = resolve_screenshot_targets(
            changed_files=changed, include_admin=True
        )
        branch = pr["head"]["ref"]
        prefix = f".agent/screenshots/pr-{pr_number}"
        if not targets:
            body_shots = (
                "### reviewer_screenshots_pre\n"
                "- production: skipped pre-merge "
                "(saberistic.com shots are post-deploy only)\n"
                "- routes (PR-affected): (none)\n"
                "- note: no pages affected by this PR "
                "(tests/docs/scripts only); screenshots skipped\n"
            )
            comment_on_issue_or_pr(repo, pr_number, body_shots)
            comment_on_issue_or_pr(repo, issue, body_shots)
            screenshot_note = (
                "- screenshots_pre: skipped (no pages affected)\n"
                "- visual_readability: `n/a`\n"
            )
        else:
            dual = capture_pre_dual(out_dir, targets=targets)
            branch_urls = upload_to_branch(repo, branch, dual.branch_paths, prefix)
            body_shots = comment_markdown_pre_dual(
                branch_url=dual.branch_url,
                branch_urls=branch_urls,
                targets=targets,
                captured=dual.branch_captured,
            )
            comment_on_issue_or_pr(repo, pr_number, body_shots)
            comment_on_issue_or_pr(repo, issue, body_shots)
            route_labels = [
                f"`{t.route}`"
                + (f" (HTTP {t.expected_status})" if t.expected_status != 200 else "")
                for t in targets
            ]
            screenshot_note = (
                f"- screenshots_pre: {len(branch_urls)} branch posted on PR + issue "
                "(no saberistic.com pre-merge shots)\n"
                f"- screenshots_routes: {', '.join(route_labels)}\n"
                f"- screenshots_branch: `{dual.branch_url}`\n"
            )
            overflow_fail = format_overflow_hard_fail(dual.overflows)
            if overflow_fail:
                hard_fail_reasons.append(overflow_fail)
                screenshot_note += (
                    f"- visual_readability: `fail` ({len(dual.overflows)} overflow(s) "
                    "on PR branch)\n"
                )
            else:
                screenshot_note += "- visual_readability: `ok` (PR branch)\n"
            empty_fail = format_empty_data_hard_fail(dual.empty_pages)
            if empty_fail:
                hard_fail_reasons.append(empty_fail)
                screenshot_note += (
                    f"- preview_mock_data: `fail` ({len(dual.empty_pages)} empty "
                    "shell finding(s) on PR branch)\n"
                )
            else:
                screenshot_note += "- preview_mock_data: `ok` (PR branch)\n"
            nav_fail = format_admin_nav_hard_fail(dual.nav_failures)
            if nav_fail:
                hard_fail_reasons.append(nav_fail)
                screenshot_note += (
                    f"- admin_nav_visible: `fail` ({len(dual.nav_failures)} "
                    "desktop finding(s) on PR branch)\n"
                )
            else:
                screenshot_note += "- admin_nav_visible: `ok` (PR branch)\n"
        pr = api("GET", f"/repos/{owner}/{name}/pulls/{pr_number}")
        sha = pr["head"]["sha"]
    except Exception as exc:
        screenshot_note = f"- screenshots_pre: failed (`{exc}`)\n"
        if os.environ.get("SCREENSHOTS_REQUIRED", "true").lower() in {"1", "true", "yes"}:
            hard_fail_reasons.append(f"required deploy screenshots failed: {exc}")

    # OpenAI / Models AI review (required for approve path).
    ai_block = ""
    ai_verdict: dict[str, Any] = {}
    try:
        from review_models import ai_review

        ai_verdict = ai_review(repo, issue, pr_number)
        ai_block += (
                    f"- ai_provider: `cursor-or-openai-or-models`\n"
            f"- ai_model: `{ai_verdict.get('model')}`\n"
            f"- ai_decision: `{ai_verdict.get('decision')}`\n"
            f"- ai_summary: {ai_verdict.get('summary')}\n"
            "- ai_reasons:\n"
            + "\n".join(f"  - {r}" for r in (ai_verdict.get("reasons") or []))
            + "\n"
        )
        if ai_verdict.get("decision") != "approved":
            hard_fail_reasons.append(
                "AI reviewer rejected: "
                + "; ".join(ai_verdict.get("reasons") or ["does not meet acceptance"])
            )
    except Exception as exc:
        hard_fail_reasons.append(f"AI reviewer unavailable: {exc}")
        ai_block += f"- ai_review: failed (`{exc}`)\n"

    # Acceptance criteria checklist with evidence (required before approve)
    acceptance_note = ""
    try:
        from acceptance import post_checklist, update_issue_checkboxes, verify_acceptance

        acceptance = verify_acceptance(repo, issue, pr_number, use_ai=True)
        product_incomplete = any(
            i.get("status") not in {"done", "n/a"} and i.get("method") != "ai-error"
            for i in (acceptance.get("items") or [])
        )
        infra_only_gap = bool(acceptance.get("ai_infra_failed")) and not product_incomplete
        if (
            infra_only_gap
            and not acceptance.get("all_done")
            and ai_verdict.get("decision") == "approved"
        ):
            # Defer to AI review so Cursor prose/JSON glitches do not bounce Builder.
            acceptance = dict(acceptance)
            acceptance["all_done"] = True
            acceptance_note += (
                "- acceptance_ai_infra: `deferred_to_ai_review` "
                "(checklist AI unavailable; AI review approved)\n"
            )
        checklist_comment = post_checklist(
            repo, issue, acceptance, role="reviewer"
        )
        acceptance_note += (
            f"- acceptance_all_done: `{str(bool(acceptance.get('all_done'))).lower()}`\n"
            f"- acceptance_checklist: {checklist_comment.get('html_url')}\n"
        )
        if acceptance.get("all_done"):
            try:
                update_issue_checkboxes(repo, issue, acceptance)
            except Exception as body_exc:
                acceptance_note += f"- acceptance_body_update: failed (`{body_exc}`)\n"
        elif product_incomplete:
            hard_fail_reasons.append(
                "acceptance criteria incomplete — see acceptance_checklist comment "
                + (checklist_comment.get("html_url") or "")
            )
        elif infra_only_gap:
            hard_fail_reasons.append(
                "acceptance AI unavailable and AI review did not approve — see "
                + (checklist_comment.get("html_url") or "")
            )
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
            f"{coverage_note}"
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
            f"{coverage_note}"
            f"{ai_block}"
            f"{acceptance_note}"
            "- judgment: AI + checks + acceptance checklist + coverage agree; "
            "nits non-blocking\n"
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
