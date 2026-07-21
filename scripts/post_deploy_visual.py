#!/usr/bin/env python3
"""After deploy: capture post-deploy screenshots and record deploy health.

Screenshots and health are posted as evidence for a human to review — this
script no longer runs an automated visual pass/fail check (see #372-class
conflicts and false negatives on non-visual changes; verification is now a
manual admin step).
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
from screenshot_deploy import (
    PRE_BRANCH_PHASE,
    capture,
    fetch_pr_changed_paths,
    resolve_base_url,
    resolve_screenshot_routes,
    upload_to_branch,
    wait_healthy,
    comment_markdown,
)

RECORD_COMMIT_PREFIX = "deploy: record post-deploy artifacts"


def record_branch_name(sha: str) -> str:
    """Deterministic per-deploy branch so a rerun for the same deploy reuses
    one branch/PR instead of opening a duplicate (mirrors
    ``freeze_shipped_migrations.freeze_branch_name``)."""
    short = (sha or "local")[:12] or "local"
    return f"deploy/screenshots-{short}"


def record_pr_body(short: str, base_url: str) -> str:
    return (
        "### deploy_record\n"
        f"- deploy: `{base_url}`\n"
        f"- sha: `{short}`\n"
        "- Automated, evidence-only change: records this deploy's `/health` "
        "snapshot and screenshot uploads under `.agent/`. No application "
        "code, migration, or test changes.\n"
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
    see the same before/after screenshots inline, not just a generic PR body
    or a raw file diff) and, when present, to the linked issue."""
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
    issue: int | None = None,
    pr_number: int | None = None,
) -> dict[str, str]:
    """Persist post-merge deployment-health evidence (#280)."""
    from crm_deploy_health import run_verification

    result = run_verification(
        repo=repo,
        sha=sha or "local",
        base_url=base_url,
        branch=branch,
        issue=issue,
        pr_number=pr_number,
        deployment={
            "api_result": "pass",
            "service_id": (os.environ.get("RENDER_SERVICE_ID") or "").strip() or None,
        },
        persist=True,
        post_comment=False,
    )
    record = result["record"]
    artifact = result.get("artifact") or {}
    slim = record.get("application_health") or {
        k: v for k, v in health.items() if not str(k).startswith("_")
    }
    raw_url = artifact.get("url") or ""

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write("### Deploy health\n\n")
            fh.write(f"- base: `{base_url}`\n")
            fh.write(f"- sha: `{record.get('sha')}`\n")
            fh.write(f"- result: `{record.get('result')}`\n")
            fh.write(
                "- post_deploy_functional_health: "
                f"`{record.get('verification_layers', {}).get('post_deploy_functional_health')}`\n"
            )
            fh.write(f"- value: `{json.dumps(slim, separators=(',', ':'))}`\n")
            if raw_url:
                fh.write(f"- recorded: {raw_url}\n")

    print(f"deploy_health={json.dumps(slim, separators=(',', ':'))}")
    if raw_url:
        print(f"deploy_health_url={raw_url}")
    return {
        "path": artifact.get("path") or f".agent/deploy/{(sha or 'local')[:12]}/deploy-health.json",
        "url": raw_url,
        "json": json.dumps(slim),
        "record": record,
        "ok": bool(result.get("ok")),
    }


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


def list_branch_pre_urls(repo: str, ref: str, pr: int | None) -> list[str]:
    """Return PR-branch pre-merge preview shot URLs (``branch-*.png``)."""
    owner, name = split_repo(repo)
    if not pr:
        return []
    prefix = f".agent/screenshots/pr-{pr}"
    try:
        nodes = api("GET", f"/repos/{owner}/{name}/contents/{prefix}?ref={ref}") or []
    except GitHubError:
        return []
    if not isinstance(nodes, list):
        return []
    urls = []
    for node in nodes:
        path = node.get("path") or ""
        name_part = path.rsplit("/", 1)[-1]
        if name_part.startswith(f"{PRE_BRANCH_PHASE}-") and name_part.endswith(".png"):
            # Prefer desktop public shots for visual compare; skip admin on prod compare.
            if "-admin" in name_part:
                continue
            urls.append(
                f"https://raw.githubusercontent.com/{owner}/{name}/{ref}/{path}"
            )
    return urls


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
            issue=issue_num or None,
            pr_number=args.pr or None,
        )
        if not health_rec.get("ok", True):
            if issue_num:
                post_issue_comment(
                    args.repo,
                    issue_num,
                    (
                        "### deploy_health_check\n"
                        f"- result: `fail`\n"
                        f"- sha: `{args.sha}`\n"
                        "- note: post-deploy CRM smoke checks failed; "
                        "completion blocked until healthy or rolled back.\n"
                    ),
                )
            print("deploy health checks failed", file=sys.stderr)
            return 1

        out = Path("trace/screenshots-post")
        changed: list[str] | None = None
        if args.pr:
            changed = fetch_pr_changed_paths(args.repo, args.pr)
        elif args.sha:
            # Resolve merged PR(s) for this deploy SHA and union their files.
            try:
                prs = api("GET", f"/repos/{owner}/{name}/commits/{args.sha}/pulls") or []
            except GitHubError:
                prs = []
            paths: list[str] = []
            for pr in prs if isinstance(prs, list) else []:
                num = pr.get("number")
                if num:
                    paths.extend(fetch_pr_changed_paths(args.repo, int(num)))
            changed = paths or None
        routes = resolve_screenshot_routes(
            changed_files=changed, include_admin=False
        )
        if not routes and changed is not None:
            post_files: list = []
            post_urls: list[str] = []
        else:
            post_files = capture(
                base_url,
                out,
                phase="post",
                routes=routes if changed is not None else None,
                allow_admin=False,
            ).paths
            prefix = (
                f".agent/screenshots/issue-{issue_num}/post"
                if issue_num
                else f".agent/screenshots/deploy-{short}/post"
            )
            post_urls = (
                upload_to_branch(args.repo, record_branch, post_files, prefix)
                if post_files
                else []
            )

        health_line = (
            f"- health: `{json.dumps(health_slim, separators=(',', ':'))}`"
            + (f" ([recorded]({health_rec['url']}))" if health_rec.get("url") else "")
        )

        record_pr = open_or_reuse_record_pr(
            args.repo, record_branch, default, short=short, base_url=base_url
        )

        if not issue_num:
            # No linked issue to comment on — post the evidence to the
            # record PR itself so its CODEOWNER reviewer sees the screenshots
            # (not just a generic PR body) before approving auto-merge.
            notify_deploy(
                args.repo,
                None,
                record_pr["number"],
                comment_markdown("### deploy_record", base_url, post_urls, extra=[health_line]),
            )
            print(
                "No issue number in commit message / linked PR; "
                f"uploaded post screenshots: {post_urls}; {health_line}"
            )
            print(
                "Tip: include `Closes #N` or `(#N)` in the commit/PR body "
                "so Reviewer gets deploy_visual_check on the issue."
            )
            print(f"record_pr={record_pr['url']}")
            return 0

        # Before shots are PR-branch previews (no saberistic.com pre-merge).
        pre_files = sorted(
            p
            for p in Path("trace/screenshots").glob("branch-*.png")
            if "-admin" not in p.name
        )
        pre_urls = (
            list_branch_pre_urls(args.repo, default, args.pr) if not pre_files else []
        )
        if pre_files:
            pre_urls = upload_to_branch(
                args.repo,
                record_branch,
                pre_files,
                f".agent/screenshots/issue-{issue_num}/pre",
            )

        if not post_files and changed is not None:
            body = (
                "### deploy_visual_check\n"
                f"- deploy: `{base_url}`\n"
                "- phase: `post-deploy`\n"
                f"- issue: #{issue_num}\n"
                f"{health_line}\n"
                "- routes (public, PR-affected): (none)\n"
                "- note: no public pages affected; screenshots skipped\n"
            )
            notify_deploy(args.repo, issue_num, record_pr["number"], body)
        else:
            # No automated pass/fail here — an admin reviews the linked
            # screenshots manually (see #372-class evidence-PR conflicts and
            # false negatives on non-visual/backend-only changes).
            extra = [
                "- phase: `post-deploy`",
                f"- issue: #{issue_num}",
                health_line,
                "- note: visual verification is manual — review the screenshots below.",
            ]
            if routes and changed is not None:
                extra.insert(
                    2,
                    "- routes (public, PR-affected): "
                    + ", ".join(f"`{r}`" for r in routes),
                )
            body = comment_markdown(
                "### deploy_visual_check",
                base_url,
                post_urls,
                extra=extra,
            )
            if pre_urls:
                pre_lines = ["\n#### Pre-merge branch screenshots"]
                for u in pre_urls:
                    name = u.rsplit("/", 1)[-1]
                    pre_lines.append(f"- **{name}**")
                    pre_lines.append(f"  ![]({u})")
                body += "\n".join(pre_lines) + "\n"
            notify_deploy(args.repo, issue_num, record_pr["number"], body)

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
                    "post": post_urls,
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
