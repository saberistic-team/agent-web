#!/usr/bin/env python3
"""Fail-closed GitHub collaborator permission check for the agent gate.

Always records a line in trace/agent-trace.jsonl and posts an issue comment
so the result is visible in the GitHub UI/API (never silent).
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from github_api import GitHubError, api, post_issue_comment, token

LEVEL_RANK = {
    "none": 0,
    "read": 1,
    "triage": 2,
    "write": 3,
    "maintain": 4,
    "admin": 5,
}

TRACE_PATH = Path("trace/agent-trace.jsonl")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def append_trace(record: dict) -> None:
    TRACE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with TRACE_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")


def github_permission(repo: str, actor: str) -> str:
    owner, name = repo.split("/", 1)
    path = (
        f"/repos/{owner}/{name}/collaborators/"
        f"{urllib.parse.quote(actor)}/permission"
    )
    payload = api("GET", path)
    level = (payload.get("permission") or "").lower()
    if level not in LEVEL_RANK:
        raise GitHubError(f"unknown permission value from API: {level!r}")
    return level


def meets_minimum(actual: str, required: str) -> bool:
    return LEVEL_RANK[actual] >= LEVEL_RANK[required]


def comment_body(record: dict) -> str:
    lines = [
        "### permission_check",
        f"- result: `{record['result']}`",
        f"- actor: `{record['actor']}`",
        f"- repo: `{record['repo']}`",
        f"- required: `{record['required']}`",
        f"- actual: `{record.get('actual')}`",
        f"- ts: `{record['ts']}`",
    ]
    if record.get("detail"):
        lines.append(f"- detail: {record['detail']}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--actor", required=True, help="GitHub login to check")
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--issue", required=True, type=int, help="Issue number")
    parser.add_argument(
        "--min-level",
        required=True,
        choices=sorted(LEVEL_RANK, key=LEVEL_RANK.get),
        help="Minimum required permission level",
    )
    args = parser.parse_args(argv)

    record = {
        "ts": utc_now(),
        "event": "permission_check",
        "result": "fail",
        "actor": args.actor,
        "repo": args.repo,
        "issue": args.issue,
        "required": args.min_level,
        "actual": None,
        "detail": None,
    }

    def persist_and_comment() -> None:
        try:
            append_trace(record)
        except OSError as exc:
            print(f"trace write failed: {exc}", file=sys.stderr)
        try:
            # Collaborator permission lookup uses GITHUB_TOKEN (needs push/admin).
            # Visible comment uses COMMENT_TOKEN when set (role App identity).
            token()
            post_issue_comment(args.repo, args.issue, comment_body(record))
        except Exception as exc:  # fail closed if we cannot leave a visible event
            print(f"FAIL: could not post permission_check comment: {exc}", file=sys.stderr)
            raise

    try:
        actual = github_permission(args.repo, args.actor)
        record["actual"] = actual
        if meets_minimum(actual, args.min_level):
            record["result"] = "pass"
            persist_and_comment()
            print(
                f"PASS: {args.actor} has {actual} on {args.repo} "
                f"(required {args.min_level})"
            )
            return 0
        record["detail"] = "insufficient permission"
        persist_and_comment()
        print(
            f"FAIL: {args.actor} has {actual} on {args.repo} "
            f"(required {args.min_level})",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:
        record["detail"] = str(exc)
        try:
            persist_and_comment()
        except Exception:
            try:
                append_trace(record)
            except OSError as trace_exc:
                print(f"trace write failed: {trace_exc}", file=sys.stderr)
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
