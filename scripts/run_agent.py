#!/usr/bin/env python3
"""Role runner that only mutates GitHub via the API (visible audit trail).

Uses GITHUB_TOKEN (expected to be the role's GitHub App installation token).
Every role posts issue comments for start/finish. No silent local-only outcomes.
"""

from __future__ import annotations

import argparse
import os
import re
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

    # Independent change areas: markdown sections or task list items
    tasks = re.findall(r"(?m)^\s*[-*] \[.\] (.+)$", body)
    sections = re.findall(r"(?m)^##\s+(.+)$", body)
    areas = tasks or sections

    agent_label = "agent:docs" if type_label == "type:docs" else "agent:builder"

    if len(areas) > 1:
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


def role_builder(repo: str, issue: int, brief: Path) -> None:
    data = get_issue(repo, issue)
    title = data.get("title") or f"issue-{issue}"
    body = data.get("body") or ""
    branch = f"builder/{issue}-{slugify(title)}"
    owner, name = split_repo(repo)
    default = api("GET", f"/repos/{owner}/{name}").get("default_branch") or "main"
    ref = api("GET", f"/repos/{owner}/{name}/git/ref/heads/{default}")
    base_sha = ref["object"]["sha"]

    # Create branch via API (visible ref)
    try:
        api(
            "POST",
            f"/repos/{owner}/{name}/git/refs",
            body={"ref": f"refs/heads/{branch}", "sha": base_sha},
        )
    except GitHubError as exc:
        if "Reference already exists" not in str(exc):
            raise

    worklog = textwrap.dedent(
        f"""\
        # Builder worklog for #{issue}

        Title: {title}

        ## Acceptance

        {body.strip() or '_No issue body provided._'}

        ## Notes

        Autonomous code generation is not wired in this runner yet.
        This commit/PR is the visible handoff artifact for review.
        """
    )
    path = f".agent/worklogs/{issue}.md"
    content_b64 = __import__("base64").b64encode(worklog.encode()).decode()
    # create or update file on branch
    put_body = {
        "message": f"builder(#{issue}): add worklog",
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
                "title": f"builder: {title} (#{issue})",
                "head": branch,
                "base": default,
                "body": (
                    f"Closes #{issue}\n\n"
                    f"Built by `agent:builder` using brief `{brief}`.\n\n"
                    f"### Worklog\nSee `{path}` on this branch.\n"
                ),
            },
        )
        post_issue_comment(
            repo,
            issue,
            f"### builder_result\n- branch: `{branch}`\n- pr: #{pr['number']}\n",
        )
    else:
        post_issue_comment(
            repo,
            issue,
            f"### builder_result\n- branch: `{branch}`\n- existing_pr: #{prs[0]['number']}\n",
        )


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

    # Missing tests heuristic: PR touches code but no test path in files
    files = api("GET", f"/repos/{owner}/{name}/pulls/{pr_number}/files") or []
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
    else:
        event = "APPROVE"
        body = (
            "### reviewer_decision\n"
            f"- decision: `approved`\n"
            f"- brief: `{brief}`\n"
            "- judgment: no hard fails; nits non-blocking\n"
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
