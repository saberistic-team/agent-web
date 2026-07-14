#!/usr/bin/env python3
"""Dispatch the highest-priority queued issue to its owning agent.

Queued work carries ``status:queued`` + ``type:*`` + ``priority:*`` without
``agent:builder`` / ``agent:docs``. This script lists those issues, sorts by
priority (critical → high → normal → low), then issue number, and applies the
intended agent label when that agent is not already ``status:in-progress``.
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
    post_issue_comment,
    split_repo,
)
from priority import (
    DEFAULT_PRIORITY,
    has_duplicate_priority_labels,
    intended_agent_label,
    is_awaiting_dispatch,
    priority_from_labels,
    priority_labels_on_issue,
    priority_sort_key,
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


def list_awaiting_dispatch(repo: str) -> list[dict[str, Any]]:
    issues = search_issues(repo, 'label:"status:queued"')
    awaiting = [issue for issue in issues if is_awaiting_dispatch(_label_names(issue))]
    awaiting.sort(
        key=lambda issue: priority_sort_key(_label_names(issue), int(issue["number"]))
    )
    return awaiting


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


def dispatch_next(repo: str, *, dry_run: bool = False) -> dict[str, Any]:
    """Dispatch at most one builder and one docs issue per run."""
    awaiting = list_awaiting_dispatch(repo)
    dispatched: list[dict[str, Any]] = []
    skipped_busy: list[dict[str, Any]] = []
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

        priority = (
            DEFAULT_PRIORITY
            if dry_run and not priority_from_labels(labels)
            else ensure_priority(repo, issue)
        )
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
                    f"- agent: `{agent}`\n"
                    "- reason: highest-priority queued work for this agent\n"
                ),
            )
        dispatched.append(
            {"issue": number, "agent": agent, "priority": priority}
        )
        busy_agents.add(agent)

    return {
        "awaiting": [int(i["number"]) for i in awaiting],
        "dispatched": dispatched,
        "skipped_busy": skipped_busy,
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
