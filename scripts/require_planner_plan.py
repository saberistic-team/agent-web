#!/usr/bin/env python3
"""Fail closed unless the issue has a ### planner_plan comment (GitHub-visible)."""

from __future__ import annotations

import argparse
import sys

from github_api import list_issue_comments


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--issue", required=True, type=int)
    args = parser.parse_args(argv)
    comments = list_issue_comments(args.repo, args.issue)
    if any("### planner_plan" in (c.get("body") or "") for c in comments):
        print("PASS: planner_plan comment present")
        return 0
    print("FAIL: missing ### planner_plan issue comment", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
