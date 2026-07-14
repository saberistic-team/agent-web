#!/usr/bin/env python3
"""Audit issues for duplicate priority:* labels and optionally normalize them.

Usage:
  python scripts/audit_priority_labels.py --repo owner/name
  python scripts/audit_priority_labels.py --repo owner/name --fix
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
from typing import Any

from dispatch_queue import replace_priority_label
from github_api import GitHubError, api, split_repo
from priority import (
    has_duplicate_priority_labels,
    priority_labels_on_issue,
    resolve_priority_label,
)


def repo_from_env() -> str:
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        raise GitHubError("GITHUB_REPOSITORY is required")
    return repo


def list_all_issues(repo: str) -> list[dict[str, Any]]:
    owner, name = split_repo(repo)
    issues: list[dict[str, Any]] = []
    page = 1
    while page <= 10:
        encoded = urllib.parse.urlencode(
            {"state": "all", "per_page": 100, "page": page}
        )
        batch = api("GET", f"/repos/{owner}/{name}/issues?{encoded}") or []
        issues.extend(item for item in batch if "pull_request" not in item)
        if len(batch) < 100:
            break
        page += 1
    return issues


def audit_issues(repo: str) -> list[dict[str, Any]]:
    duplicates: list[dict[str, Any]] = []
    for issue in list_all_issues(repo):
        labels = {label["name"] for label in issue.get("labels") or []}
        if not has_duplicate_priority_labels(labels):
            continue
        duplicates.append(
            {
                "issue": int(issue["number"]),
                "state": issue.get("state"),
                "title": issue.get("title"),
                "priorities": priority_labels_on_issue(labels),
                "resolved": resolve_priority_label(
                    issue.get("title") or "",
                    issue.get("body") or "",
                    labels,
                ),
            }
        )
    return duplicates


def normalize_duplicates(repo: str, duplicates: list[dict[str, Any]]) -> None:
    for item in duplicates:
        replace_priority_label(repo, int(item["issue"]), item["resolved"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=None, help="owner/name")
    parser.add_argument(
        "--fix",
        action="store_true",
        help="Replace duplicate priority:* labels with the resolved canonical value",
    )
    args = parser.parse_args(argv)

    try:
        repo = args.repo or repo_from_env()
        duplicates = audit_issues(repo)
        if args.fix and duplicates:
            normalize_duplicates(repo, duplicates)
    except Exception as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    print(
        json.dumps(
            {
                "repo": repo,
                "duplicate_count": len(duplicates),
                "duplicates": duplicates,
                "fixed": bool(args.fix and duplicates),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
