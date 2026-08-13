#!/usr/bin/env python3
"""Resolve exactly one intentionally linked open pull request for an issue."""

from __future__ import annotations

import argparse
import json
import sys

from github_api import GitHubError, IssuePRResolutionError, resolve_issue_pr, issue_pr_resolution


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--issue", required=True, type=int)
    args = parser.parse_args(argv)
    try:
        result = resolve_issue_pr(args.repo, args.issue)
    except IssuePRResolutionError as exc:
        print(json.dumps(exc.resolution, sort_keys=True))
        return 2
    except GitHubError as exc:
        print(json.dumps({"repository": args.repo, "issue_number": args.issue, "error": str(exc)}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
