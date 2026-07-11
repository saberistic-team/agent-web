#!/usr/bin/env python3
"""Fail-closed GitHub collaborator permission check for the agent gate."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

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


def github_permission(repo: str, actor: str, token: str) -> str:
    """Return current permission level from the live Collaborators API."""
    if "/" not in repo:
        raise ValueError(f"repo must be owner/name, got {repo!r}")
    owner, name = repo.split("/", 1)
    url = (
        f"https://api.github.com/repos/{owner}/{name}"
        f"/collaborators/{urllib.parse.quote(actor)}/permission"
    )
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "agent-web-check-permission",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.load(resp)
    level = (payload.get("permission") or "").lower()
    if level not in LEVEL_RANK:
        raise ValueError(f"unknown permission value from API: {level!r}")
    return level


def meets_minimum(actual: str, required: str) -> bool:
    return LEVEL_RANK[actual] >= LEVEL_RANK[required]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--actor", required=True, help="GitHub login to check")
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument(
        "--min-level",
        required=True,
        choices=sorted(LEVEL_RANK, key=LEVEL_RANK.get),
        help="Minimum required permission level",
    )
    args = parser.parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    record = {
        "ts": utc_now(),
        "event": "permission_check",
        "result": "fail",
        "actor": args.actor,
        "repo": args.repo,
        "required": args.min_level,
        "actual": None,
        "detail": None,
    }

    if not token:
        record["detail"] = "missing GITHUB_TOKEN"
        try:
            append_trace(record)
        except OSError as exc:
            print(f"trace write failed: {exc}", file=sys.stderr)
        print("FAIL: missing GITHUB_TOKEN", file=sys.stderr)
        return 1

    try:
        actual = github_permission(args.repo, args.actor, token)
        record["actual"] = actual
        if meets_minimum(actual, args.min_level):
            record["result"] = "pass"
            append_trace(record)
            print(
                f"PASS: {args.actor} has {actual} on {args.repo} "
                f"(required {args.min_level})"
            )
            return 0
        record["detail"] = "insufficient permission"
        append_trace(record)
        print(
            f"FAIL: {args.actor} has {actual} on {args.repo} "
            f"(required {args.min_level})",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:  # fail closed on any error
        record["detail"] = str(exc)
        try:
            append_trace(record)
        except OSError as trace_exc:
            print(f"trace write failed: {trace_exc}", file=sys.stderr)
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
