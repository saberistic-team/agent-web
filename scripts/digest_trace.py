#!/usr/bin/env python3
"""Summarize agent-trace.jsonl lines into a stakeholder-friendly markdown digest."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def load_records(paths: Iterable[Path]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    records: list[dict[str, Any]] = []
    for path in paths:
        if path.is_dir():
            files = sorted(path.rglob("*.jsonl"))
        else:
            files = [path]
        for file in files:
            if not file.is_file():
                continue
            for line in file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                if line in seen:
                    continue
                seen.add(line)
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return records


def within_window(records: list[dict[str, Any]], since: datetime) -> list[dict[str, Any]]:
    out = []
    for row in records:
        ts = parse_ts(row.get("ts"))
        if ts is None:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts >= since:
            out.append(row)
    return out


def money(value: Any) -> float:
    try:
        if value is None:
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def render_digest(rows: list[dict[str, Any]], *, since: datetime, until: datetime) -> str:
    by_role: Counter[str] = Counter()
    by_outcome: Counter[str] = Counter()
    cost_by_role: dict[str, float] = defaultdict(float)
    fails: list[dict[str, Any]] = []
    issues: set[int] = set()

    total_cost = 0.0
    for row in rows:
        role = str(row.get("role") or "unknown")
        outcome = str(row.get("outcome") or "unknown")
        by_role[role] += 1
        by_outcome[outcome] += 1
        cost = money(row.get("cost_usd"))
        total_cost += cost
        cost_by_role[role] += cost
        if row.get("issue") is not None:
            try:
                issues.add(int(row["issue"]))
            except (TypeError, ValueError):
                pass
        if outcome == "fail":
            fails.append(row)

    since_s = since.strftime("%Y-%m-%d")
    until_s = until.strftime("%Y-%m-%d")

    lines = [
        "## Agent activity — weekly digest",
        "",
        f"_Window (UTC): **{since_s}** → **{until_s}**_",
        "",
        "### Snapshot",
        "",
        f"| Metric | Value |",
        f"| --- | --- |",
        f"| Actions recorded | **{len(rows)}** |",
        f"| Issues touched | **{len(issues)}** |",
        f"| Estimated cost (USD) | **${total_cost:.4f}** |",
        f"| Failures | **{by_outcome.get('fail', 0)}** |",
        "",
        "### By role",
        "",
        "| Role | Actions | Est. cost (USD) |",
        "| --- | ---: | ---: |",
    ]

    if by_role:
        for role, count in by_role.most_common():
            lines.append(f"| `{role}` | {count} | ${cost_by_role[role]:.4f} |")
    else:
        lines.append("| _(none)_ | 0 | $0.0000 |")

    lines.extend(
        [
            "",
            "### Outcomes",
            "",
            "| Outcome | Count |",
            "| --- | ---: |",
        ]
    )
    if by_outcome:
        for outcome, count in by_outcome.most_common():
            lines.append(f"| `{outcome}` | {count} |")
    else:
        lines.append("| _(none)_ | 0 |")

    lines.extend(["", "### Failures (if any)", ""])
    if not fails:
        lines.append("_No failed actions in this window._")
    else:
        lines.append("| When (UTC) | Role | Issue | Action |")
        lines.append("| --- | --- | ---: | --- |")
        for row in sorted(fails, key=lambda r: str(r.get("ts") or ""))[-25:]:
            ts = str(row.get("ts") or "")[:19].replace("T", " ")
            role = row.get("role") or ""
            issue = row.get("issue") if row.get("issue") is not None else "—"
            action = row.get("action") or ""
            lines.append(f"| {ts} | `{role}` | {issue} | `{action}` |")
        if len(fails) > 25:
            lines.append("")
            lines.append(f"_Showing latest 25 of {len(fails)} failures._")

    if not rows:
        lines.extend(
            [
                "",
                "> No trace lines were found for this window. If agents ran, check that",
                "> `agent-trace-*` workflow artifacts are still available (90-day default)",
                "> or that lines were merged into `trace/agent-trace.jsonl` on `main`.",
            ]
        )

    lines.extend(["", "—", "_Posted by the weekly trace digest workflow._", ""])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        action="append",
        dest="inputs",
        type=Path,
        required=True,
        help="JSONL file or directory (repeatable)",
    )
    parser.add_argument("--days", type=int, default=7, help="Lookback window in days")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Write markdown here (default: stdout)",
    )
    args = parser.parse_args(argv)

    until = datetime.now(timezone.utc)
    since = until - timedelta(days=args.days)
    records = within_window(load_records(args.inputs), since)
    markdown = render_digest(records, since=since, until=until)

    if args.output:
        args.output.write_text(markdown, encoding="utf-8")
    print(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
