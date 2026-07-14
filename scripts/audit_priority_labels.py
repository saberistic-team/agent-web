#!/usr/bin/env python3
"""Audit issues for duplicate priority:* labels and optionally normalize them.

Normalization keeps one label via ``resolve_priority_label`` (non-default beats
``priority:normal``; otherwise highest urgency wins). Ambiguous duplicates are
reported without mutation.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
from typing import Any

from github_api import GitHubError, add_labels, api, delete_label, split_repo
from priority import all_priority_labels, resolve_priority_label


def repo_from_env() -> str:
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        raise GitHubError("GITHUB_REPOSITORY is required")
    return repo


def list_repo_issues(repo: str, *, state: str) -> list[dict[str, Any]]:
    owner, name = split_repo(repo)
    issues: list[dict[str, Any]] = []
    page = 1
    while page <= 20:
        encoded = urllib.parse.urlencode(
            {"state": state, "per_page": 100, "page": page, "labels": ""}
        )
        batch = api("GET", f"/repos/{owner}/{name}/issues?{encoded}") or []
        issues.extend(item for item in batch if "pull_request" not in item)
        if len(batch) < 100:
            break
        page += 1
    return issues


def normalize_issue_priority(
    repo: str, issue: dict[str, Any], *, dry_run: bool = False
) -> dict[str, Any] | None:
    number = int(issue["number"])
    labels = {label["name"] for label in issue.get("labels") or []}
    priorities = all_priority_labels(labels)
    if len(priorities) <= 1:
        return None

    resolved = resolve_priority_label(priorities)
    entry: dict[str, Any] = {
        "issue": number,
        "title": issue.get("title") or "",
        "state": issue.get("state") or "",
        "priorities": priorities,
        "resolved": resolved,
    }
    if resolved is None:
        entry["action"] = "report_ambiguous"
        return entry

    entry["action"] = "normalize"
    entry["removed"] = [label for label in priorities if label != resolved]
    entry["kept"] = resolved
    if dry_run:
        return entry

    for label in priorities:
        if label != resolved:
            delete_label(repo, number, label)
    if resolved not in labels:
        add_labels(repo, number, [resolved])
    return entry


def audit_repo(repo: str, *, fix: bool = False, dry_run: bool = False) -> dict[str, Any]:
    duplicates: list[dict[str, Any]] = []
    for state in ("open", "closed"):
        for issue in list_repo_issues(repo, state=state):
            entry = normalize_issue_priority(repo, issue, dry_run=dry_run or not fix)
            if entry is not None:
                duplicates.append(entry)
    return {
        "repo": repo,
        "duplicate_count": len(duplicates),
        "duplicates": duplicates,
        "fix": fix and not dry_run,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=None, help="owner/name (default GITHUB_REPOSITORY)")
    parser.add_argument(
        "--fix",
        action="store_true",
        help="Normalize resolvable duplicates (ambiguous ones are only reported)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what --fix would change without mutating labels",
    )
    args = parser.parse_args(argv)

    try:
        repo = args.repo or repo_from_env()
        result = audit_repo(repo, fix=args.fix, dry_run=args.dry_run)
    except Exception as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
