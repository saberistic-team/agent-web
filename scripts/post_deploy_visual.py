#!/usr/bin/env python3
"""After deploy: record deploy health.

This used to also capture and upload post-deploy production screenshots
under ``.agent/screenshots/issue-<n>/post/``, but re-running the same deploy
sha (retries, manual reruns) recaptured non-deterministic screenshots at the
same paths, and the record-PR branch is force-reset to the latest ``main``
each run — combined, that produced unresolvable binary "added in both"
conflicts on the auto-merge record PR (e.g. #372). Screenshot capture is now
Reviewer's job pre-merge only (``docs/SCREENSHOTS.md``); post-deploy just
confirms the app is healthy.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from github_api import (
    GitHubError,
    api,
    create_branch,
    enable_auto_merge,
    find_open_pr_for_branch,
    open_pull_request,
    post_issue_comment,
    split_repo,
)
from screenshot_deploy import resolve_base_url, upload_to_branch, wait_healthy

RECORD_COMMIT_PREFIX = "deploy: record post-deploy artifacts"


def record_branch_name(sha: str) -> str:
    """Deterministic per-deploy branch so a rerun for the same deploy reuses
    one branch/PR instead of opening a duplicate (mirrors
    ``freeze_shipped_migrations.freeze_branch_name``)."""
    short = (sha or "local")[:12] or "local"
    return f"deploy/health-{short}"


def record_pr_body(short: str, base_url: str) -> str:
    return (
        "### deploy_record\n"
        f"- deploy: `{base_url}`\n"
        f"- sha: `{short}`\n"
        "- Automated, evidence-only change: records this deploy's `/health` "
        "snapshot under `.agent/`. No application code, migration, or test "
        "changes.\n"
        "- Opened as a PR (not a direct push) because the workflow-governance "
        "ruleset requires every change to `main` go through review — see "
        "`docs/WORKFLOW_GOVERNANCE.md`. Auto-merge is enabled: approving this "
        "PR is sufficient, no separate merge click needed.\n"
    )


def open_or_reuse_record_pr(
    repo: str,
    head_branch: str,
    base_branch: str,
    *,
    short: str,
    base_url: str,
) -> dict[str, Any]:
    """Open (or reuse) the auto-merge PR that lands this deploy's recorded
    evidence on ``base_branch``.

    Same pattern as ``freeze_shipped_migrations.maybe_commit_freeze``: a
    direct push to a protected branch is rejected by the workflow-governance
    ruleset (issue #362), so evidence commits land on a dedicated branch and
    merge themselves via GitHub's native auto-merge the instant a human
    CODEOWNER approves. Reruns against the same deploy sha reuse the existing
    open PR instead of opening a duplicate.
    """
    existing = find_open_pr_for_branch(repo, head_branch)
    if existing is not None:
        return {"number": existing["number"], "url": existing["html_url"]}
    title = f"{RECORD_COMMIT_PREFIX} ({short})"
    pr = open_pull_request(
        repo,
        head=head_branch,
        base=base_branch,
        title=title,
        body=record_pr_body(short, base_url),
    )
    enable_auto_merge(repo, pr["node_id"])
    return {"number": pr["number"], "url": pr["html_url"]}


def notify_deploy(repo: str, issue_num: int | None, record_pr_number: int, body: str) -> None:
    """Post deploy evidence to the record PR (its CODEOWNER reviewer should
    see the same health record, not just a generic PR body) and, when
    present, to the linked issue."""
    post_issue_comment(repo, record_pr_number, body)
    if issue_num:
        post_issue_comment(repo, issue_num, body)


def record_health(
    repo: str,
    branch: str,
    *,
    sha: str,
    base_url: str,
    health: dict,
) -> dict[str, str]:
    """Persist /health JSON after every deploy (file on branch + optional summary)."""
    short = (sha or "local")[:12] or "local"
    slim = {k: v for k, v in health.items() if not str(k).startswith("_")}
    payload = {
        "sha": sha or short,
        "base_url": base_url,
        "health_url": health.get("_health_url") or f"{base_url.rstrip('/')}/health",
        "health": slim,
    }
    out = Path("trace/deploy-health.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2) + "\n"
    out.write_text(text, encoding="utf-8")

    prefix = f".agent/deploy/{short}"
    urls = upload_to_branch(
        repo, branch, [out], prefix, message=f"deploy: record health ({short})"
    )
    raw_url = urls[0] if urls else ""

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write("### Deploy health\n\n")
            fh.write(f"- base: `{base_url}`\n")
            fh.write(f"- sha: `{sha or short}`\n")
            fh.write(f"- value: `{json.dumps(slim, separators=(',', ':'))}`\n")
            if raw_url:
                fh.write(f"- recorded: {raw_url}\n")

    print(f"deploy_health={json.dumps(slim, separators=(',', ':'))}")
    if raw_url:
        print(f"deploy_health_url={raw_url}")
    return {"path": f"{prefix}/deploy-health.json", "url": raw_url, "json": json.dumps(slim)}


def find_issue_number(message: str) -> int | None:
    # Prefer explicit closes / (#N) from PR merges
    for pattern in (
        r"(?i)(?:closes|fixes|resolves)\s+#(\d+)",
        r"\(#(\d+)\)",
        r"#(\d+)",
    ):
        m = re.search(pattern, message or "")
        if m:
            return int(m.group(1))
    return None


def find_issue_from_commit(repo: str, sha: str) -> int | None:
    """Resolve issue via PRs associated with the commit (merge or squash)."""
    if not sha:
        return None
    owner, name = split_repo(repo)
    try:
        prs = api("GET", f"/repos/{owner}/{name}/commits/{sha}/pulls") or []
    except GitHubError:
        return None
    if not isinstance(prs, list):
        return None
    for pr in prs:
        blob = f"{pr.get('title') or ''}\n{pr.get('body') or ''}"
        found = find_issue_number(blob)
        if found:
            return found
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--commit-message", default="")
    parser.add_argument("--sha", default=os.environ.get("GITHUB_SHA", ""))
    parser.add_argument("--issue", type=int, default=0)
    parser.add_argument("--pr", type=int, default=0)
    parser.add_argument("--base-url", default="")
    args = parser.parse_args(argv)

    try:
        owner, name = split_repo(args.repo)
        issue_num = (
            args.issue
            or find_issue_number(args.commit_message)
            or find_issue_from_commit(args.repo, args.sha)
        )
        default = api("GET", f"/repos/{owner}/{name}").get("default_branch") or "main"
        short = (args.sha or "local")[:12] or "local"
        # Record evidence on a dedicated branch + auto-merge PR rather than
        # pushing straight to the default branch — same reason and pattern as
        # freeze_shipped_migrations.py's maybe_commit_freeze (issue #362): the
        # workflow-governance ruleset rejects direct bot pushes to a
        # protected branch with a 422.
        record_branch = record_branch_name(args.sha)
        create_branch(args.repo, record_branch, base_branch=default)
        base_url = resolve_base_url(args.base_url)
        health = wait_healthy(base_url)
        health_slim = {k: v for k, v in health.items() if not str(k).startswith("_")}
        health_rec = record_health(
            args.repo,
            record_branch,
            sha=args.sha,
            base_url=base_url,
            health=health,
        )

        health_line = (
            f"- health: `{json.dumps(health_slim, separators=(',', ':'))}`"
            + (f" ([recorded]({health_rec['url']}))" if health_rec.get("url") else "")
        )

        record_pr = open_or_reuse_record_pr(
            args.repo, record_branch, default, short=short, base_url=base_url
        )

        body_lines = [
            "### deploy_record",
            f"- deploy: `{base_url}`",
            f"- sha: `{short}`",
        ]
        if issue_num:
            body_lines.append(f"- issue: #{issue_num}")
        body_lines.append(health_line)
        notify_deploy(args.repo, issue_num, record_pr["number"], "\n".join(body_lines) + "\n")

        if not issue_num:
            # No linked issue to comment on beyond the record PR above.
            print(f"No issue number in commit message / linked PR; {health_line}")
            print(
                "Tip: include `Closes #N` or `(#N)` in the commit/PR body "
                "so the acceptance checklist refreshes on the issue."
            )
            print(f"record_pr={record_pr['url']}")
            return 0

        try:
            from acceptance import (
                post_checklist,
                update_issue_checkboxes,
                verify_acceptance,
            )

            acceptance = verify_acceptance(
                args.repo, issue_num, args.pr or None, use_ai=True
            )
            post_checklist(args.repo, issue_num, acceptance, role="post-deploy")
            if acceptance.get("all_done"):
                update_issue_checkboxes(args.repo, issue_num, acceptance)
        except Exception as acc_exc:
            post_issue_comment(
                args.repo,
                issue_num,
                f"### acceptance_checklist\n- role: `post-deploy`\n"
                f"- all_done: `false`\n- note: refresh failed (`{acc_exc}`)\n",
            )

        print(
            json.dumps(
                {
                    "issue": issue_num,
                    "record_pr": record_pr["url"],
                }
            )
        )
        return 0
    except Exception as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main())
