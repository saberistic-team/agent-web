#!/usr/bin/env python3
"""Dispatch the next queued issue to its owning agent.

Queued work carries ``status:queued`` + ``type:*`` + ``priority:*`` without
``agent:builder`` / ``agent:docs``. This script lists those issues, keeps only
issues on an **open** GitHub milestone (or ``priority:critical`` hotfixes),
skips issues with open or unstructured dependencies
(``scripts/issue_deps.py``), sorts by earliest milestone due date, then
priority, then issue number, and applies the intended agent label when that
agent is not already ``status:in-progress``.
"""

from __future__ import annotations

import argparse
import os
import sys
import urllib.parse
from typing import Any

from github_api import (
    GitHubError,
    add_labels,
    api,
    delete_label,
    list_issue_comments,
    post_issue_comment,
    split_repo,
)
from issue_deps import (
    dependency_block_reason,
    dispatcher_skip_comment,
    reconcile_comment,
    reconcile_issue_dependencies,
)
from milestones import (
    dispatch_sort_key,
    is_dispatch_eligible,
    list_open_milestones,
    open_milestone_numbers,
)
from priority import (
    DEFAULT_PRIORITY,
    has_duplicate_priority_labels,
    intended_agent_label,
    is_awaiting_dispatch,
    priority_from_labels,
    priority_labels_on_issue,
    resolve_priority_label,
)


def repo_from_env() -> str:
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        raise GitHubError("GITHUB_REPOSITORY is required")
    return repo


def _label_names(issue: dict[str, Any]) -> set[str]:
    return {label["name"] for label in issue.get("labels") or []}


def search_issues(repo: str, query: str) -> list[dict[str, Any]]:
    """Paginate GitHub issue search for this repo."""
    owner, name = split_repo(repo)
    q = f"repo:{owner}/{name} is:issue is:open {query}"
    results: list[dict[str, Any]] = []
    page = 1
    while page <= 5:
        encoded = urllib.parse.urlencode({"q": q, "per_page": 50, "page": page})
        data = api("GET", f"/search/issues?{encoded}") or {}
        items = data.get("items") or []
        results.extend(items)
        if len(items) < 50:
            break
        page += 1
    return results


def list_awaiting_dispatch(
    repo: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (eligible queued issues, skipped for closed/missing milestone)."""
    issues = search_issues(repo, 'label:"status:queued"')
    open_milestones = list_open_milestones(repo)
    open_numbers = open_milestone_numbers(open_milestones)
    by_number = {
        int(m["number"]): m for m in open_milestones if m.get("number") is not None
    }
    awaiting: list[dict[str, Any]] = []
    skipped_milestone: list[dict[str, Any]] = []
    for issue in issues:
        labels = _label_names(issue)
        if not is_awaiting_dispatch(labels):
            continue
        if not is_dispatch_eligible(issue, labels, open_numbers):
            milestone = issue.get("milestone") or {}
            skipped_milestone.append(
                {
                    "issue": int(issue["number"]),
                    "reason": "milestone_not_open",
                    "milestone": milestone.get("title") or None,
                    "milestone_number": milestone.get("number"),
                }
            )
            continue
        awaiting.append(issue)
    awaiting.sort(
        key=lambda issue: dispatch_sort_key(
            issue,
            _label_names(issue),
            open_milestones_by_number=by_number,
        )
    )
    return awaiting, skipped_milestone


def agent_in_progress(repo: str, agent_label: str) -> bool:
    issues = search_issues(repo, f'label:"status:in-progress" label:"{agent_label}"')
    return bool(issues)


def replace_priority_label(repo: str, issue_number: int, priority_label: str) -> None:
    """Replace all priority:* labels with exactly one canonical label."""
    owner, name = split_repo(repo)
    data = api("GET", f"/repos/{owner}/{name}/issues/{issue_number}") or {}
    labels = {label["name"] for label in data.get("labels") or []}
    for label in list(labels):
        if label.startswith("priority:"):
            delete_label(repo, issue_number, label)
    add_labels(repo, issue_number, [priority_label])


def ensure_priority(repo: str, issue: dict[str, Any]) -> str:
    labels = _label_names(issue)
    number = int(issue["number"])
    priority = resolve_priority_label(
        issue.get("title") or "",
        issue.get("body") or "",
        labels,
    )
    existing = priority_labels_on_issue(labels)
    if len(existing) == 1 and existing[0] == priority:
        return priority
    replace_priority_label(repo, number, priority)
    if has_duplicate_priority_labels(labels):
        post_issue_comment(
            repo,
            number,
            (
                "### dispatcher_priority_normalize\n"
                f"- issue: `#{number}`\n"
                f"- removed: {', '.join(f'`{label}`' for label in existing)}\n"
                f"- kept: `{priority}`\n"
                "- reason: multiple priority:* labels replaced with one canonical value\n"
            ),
        )
    return priority


def _recent_dispatcher_skip(repo: str, issue_number: int) -> bool:
    """True when the newest issue comment is already a deps skip (avoid cron spam)."""
    comments = list_issue_comments(repo, issue_number)
    if not comments:
        return False
    body = comments[-1].get("body") or ""
    return "### dispatcher_skip" in body and "open_dependencies" in body


def dispatch_next(repo: str, *, dry_run: bool = False) -> dict[str, Any]:
    """Dispatch at most one builder and one docs issue per run."""
    awaiting, skipped_milestone = list_awaiting_dispatch(repo)
    dispatched: list[dict[str, Any]] = []
    skipped_busy: list[dict[str, Any]] = []
    skipped_deps: list[dict[str, Any]] = []
    busy_agents: set[str] = set()

    for issue in awaiting:
        number = int(issue["number"])
        labels = _label_names(issue)
        agent = intended_agent_label(labels)
        if agent in busy_agents or agent_in_progress(repo, agent):
            busy_agents.add(agent)
            skipped_busy.append(
                {"issue": number, "agent": agent, "reason": "agent_in_progress"}
            )
            continue
        if any(item["agent"] == agent for item in dispatched):
            skipped_busy.append(
                {"issue": number, "agent": agent, "reason": "already_dispatched_this_run"}
            )
            continue

        # Derive + write missing blockedBy / parent-child / Depends-on before
        # deciding whether this queued issue may start.
        reconcile_summary: dict[str, Any]
        if dry_run:
            reconcile_summary = {
                "body": issue.get("body") or "",
                "blockers": [],
                "added_blocked_by": [],
                "added_sub_issues": [],
                "body_updated": False,
            }
            dep_reason = dependency_block_reason(
                repo, number, body=issue.get("body") or "", reconcile=False
            )
        else:
            reconcile_summary = reconcile_issue_dependencies(
                repo, number, body=issue.get("body") or "", write=True
            )
            note = reconcile_comment(reconcile_summary)
            if note:
                post_issue_comment(repo, number, note)
            dep_reason = dependency_block_reason(
                repo,
                number,
                body=str(reconcile_summary.get("body") or issue.get("body") or ""),
                reconcile=False,
            )
        if dep_reason:
            skipped_deps.append(
                {
                    "issue": number,
                    "agent": agent,
                    "reason": "open_dependencies",
                    "detail": dep_reason,
                    "reconcile": {
                        "added_blocked_by": reconcile_summary.get("added_blocked_by"),
                        "added_sub_issues": reconcile_summary.get("added_sub_issues"),
                        "body_updated": reconcile_summary.get("body_updated"),
                    },
                }
            )
            if not dry_run and not _recent_dispatcher_skip(repo, number):
                post_issue_comment(
                    repo,
                    number,
                    dispatcher_skip_comment(number, dep_reason),
                )
            continue

        priority = (
            DEFAULT_PRIORITY
            if dry_run and not priority_from_labels(labels)
            else ensure_priority(repo, issue)
        )
        milestone = issue.get("milestone") or {}
        milestone_title = milestone.get("title") or "(none)"
        if not dry_run:
            for label in list(labels):
                if label.startswith("agent:"):
                    delete_label(repo, number, label)
            add_labels(repo, number, [agent])
            post_issue_comment(
                repo,
                number,
                (
                    "### dispatcher_dispatch\n"
                    f"- issue: `#{number}`\n"
                    f"- priority: `{priority}`\n"
                    f"- milestone: `{milestone_title}`\n"
                    f"- agent: `{agent}`\n"
                    "- reason: earliest-due open-milestone work for this agent\n"
                ),
            )
        dispatched.append(
            {
                "issue": number,
                "agent": agent,
                "priority": priority,
                "milestone": milestone_title,
            }
        )
        busy_agents.add(agent)

    return {
        "awaiting": [int(i["number"]) for i in awaiting],
        "dispatched": dispatched,
        "skipped_busy": skipped_busy,
        "skipped_deps": skipped_deps,
        "skipped_milestone": skipped_milestone,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List what would be dispatched without mutating labels",
    )
    parser.add_argument(
        "--repo",
        default=None,
        help="owner/name (defaults to GITHUB_REPOSITORY)",
    )
    args = parser.parse_args(argv)

    try:
        repo = args.repo or repo_from_env()
        result = dispatch_next(repo, dry_run=args.dry_run)
    except Exception as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    import json

    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
