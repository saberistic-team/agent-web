#!/usr/bin/env python3
"""Append-only, flock-safe writer for trace/agent-trace.jsonl."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TRACE_PATH = Path("trace/agent-trace.jsonl")

SCHEMA_KEYS = (
    "ts",
    "role",
    "issue",
    "pr",
    "action",
    "model",
    "cost_usd",
    "outcome",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_event(
    *,
    role: str,
    action: str,
    outcome: str,
    issue: int | None = None,
    pr: int | None = None,
    model: str | None = None,
    cost_usd: float | None = None,
    ts: str | None = None,
    path: Path = TRACE_PATH,
) -> dict[str, Any]:
    """Append one schema-conformant line. Safe for concurrent writers on one host."""
    if issue is None and pr is None:
        raise ValueError("at least one of issue or pr is required")

    record = {
        "ts": ts or utc_now(),
        "role": role,
        "issue": issue,
        "pr": pr,
        "action": action,
        "model": model,
        "cost_usd": cost_usd,
        "outcome": outcome,
    }
    # Stable key order for readability / jq
    line = json.dumps({k: record[k] for k in SCHEMA_KEYS}, separators=(",", ":")) + "\n"

    path.parent.mkdir(parents=True, exist_ok=True)
    # a+ so we can lock a file that may not exist yet; O_APPEND alone is not
    # enough across processes without an exclusive lock around the write.
    with path.open("a+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            fh.seek(0, os.SEEK_END)
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", required=True)
    parser.add_argument("--action", required=True)
    parser.add_argument("--outcome", required=True)
    parser.add_argument("--issue", type=int, default=None)
    parser.add_argument("--pr", type=int, default=None)
    parser.add_argument(
        "--model",
        default=None,
        help="Model id if an LLM was used; omit for non-model actions",
    )
    parser.add_argument(
        "--cost-usd",
        type=float,
        default=None,
        help="Estimated USD cost for this action, if known",
    )
    parser.add_argument(
        "--trace-path",
        type=Path,
        default=TRACE_PATH,
        help="Override path (tests)",
    )
    args = parser.parse_args(argv)

    try:
        record = write_event(
            role=args.role,
            action=args.action,
            outcome=args.outcome,
            issue=args.issue,
            pr=args.pr,
            model=args.model,
            cost_usd=args.cost_usd,
            path=args.trace_path,
        )
    except Exception as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(record, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
