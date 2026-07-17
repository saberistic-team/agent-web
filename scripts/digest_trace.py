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


# Successful outcomes that count as “done” work for stakeholders.
_OK = frozenset({"ok", "pass"})

# Map trace actions → plain-language feature/work labels.
_ACTION_LABELS: dict[str, str] = {
    "plan": "Planned work",
    "release": "Release plan",
    "build": "Built / implemented",
    "docs": "Docs update",
    "dispatch": "Queued for an agent",
    "review": "Reviewed",
    "review:approved": "Review approved (screenshots + acceptance)",
    "review:changes-requested": "Review requested changes",
    "review:blocked": "Review blocked",
    "gate:merge": "Merged via gate",
    "gate:review-approved": "Merged after review approval",
    "gate:release-plan": "Gate release plan",
    "weekly_digest": "Weekly digest posted",
    "permission_check": "Permission check",
}


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


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _action_label(action: str) -> str:
    if action in _ACTION_LABELS:
        return _ACTION_LABELS[action]
    if action.startswith("gate:"):
        return f"Gate `{action.split(':', 1)[1]}`"
    if action.startswith("review:"):
        return f"Review `{action.split(':', 1)[1]}`"
    return action.replace("_", " ").capitalize() or "(unknown)"


def _issue_work(rows: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """Aggregate per-issue activity for the deliverables section."""
    by_issue: dict[int, dict[str, Any]] = {}
    for row in rows:
        issue = _int_or_none(row.get("issue"))
        if issue is None:
            continue
        bucket = by_issue.setdefault(
            issue,
            {
                "roles": set(),
                "actions": [],
                "prs": set(),
                "ok": False,
                "built": False,
                "approved": False,
                "merged": False,
                "screenshotish": False,
            },
        )
        role = str(row.get("role") or "")
        action = str(row.get("action") or "")
        outcome = str(row.get("outcome") or "")
        bucket["roles"].add(role)
        bucket["actions"].append(action)
        pr = _int_or_none(row.get("pr"))
        if pr is not None:
            bucket["prs"].add(pr)
        if outcome in _OK:
            bucket["ok"] = True
        if role == "builder" and action == "build" and outcome in _OK:
            bucket["built"] = True
        if action in {"review:approved", "review"} and outcome in _OK:
            # Approve path posts pre-merge branch screenshots (incl. admin preview).
            bucket["approved"] = action == "review:approved" or bucket["approved"]
            if action == "review:approved":
                bucket["screenshotish"] = True
        if action.startswith("review:") and outcome in _OK:
            bucket["screenshotish"] = True
        if (
            action.startswith("gate:")
            and outcome in _OK
            and action != "gate:release-plan"
            and ("merge" in action or action == "gate:review-approved")
        ):
            bucket["merged"] = True
            bucket["screenshotish"] = True  # pre-merge screenshots already posted by review
    return by_issue


def render_digest(rows: list[dict[str, Any]], *, since: datetime, until: datetime) -> str:
    by_role: Counter[str] = Counter()
    by_outcome: Counter[str] = Counter()
    cost_by_role: dict[str, float] = defaultdict(float)
    fails: list[dict[str, Any]] = []
    issues: set[int] = set()
    prs: set[int] = set()

    total_cost = 0.0
    for row in rows:
        role = str(row.get("role") or "unknown")
        outcome = str(row.get("outcome") or "unknown")
        by_role[role] += 1
        by_outcome[outcome] += 1
        cost = money(row.get("cost_usd"))
        total_cost += cost
        cost_by_role[role] += cost
        issue = _int_or_none(row.get("issue"))
        if issue is not None:
            issues.add(issue)
        pr = _int_or_none(row.get("pr"))
        if pr is not None:
            prs.add(pr)
        if outcome == "fail":
            fails.append(row)

    since_s = since.strftime("%Y-%m-%d")
    until_s = until.strftime("%Y-%m-%d")
    work = _issue_work(rows)
    features_done = sorted(
        n
        for n, meta in work.items()
        if meta["built"] or meta["approved"] or meta["merged"]
    )
    screenshot_issues = sorted(n for n, meta in work.items() if meta["screenshotish"])

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
        f"| PRs recorded | **{len(prs)}** |",
        f"| Features / issues advanced | **{len(features_done)}** |",
        f"| Issues with screenshot evidence | **{len(screenshot_issues)}** |",
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

    # --- Deliverables: issues, PRs, features, screenshots ---
    lines.extend(["", "### Issues & PRs", ""])
    if not work and not prs:
        lines.append("_No issues or PRs recorded in this window._")
    else:
        lines.append("| Issue | Roles | PR(s) | Status signals |")
        lines.append("| ---: | --- | --- | --- |")
        for issue in sorted(work):
            meta = work[issue]
            roles = ", ".join(f"`{r}`" for r in sorted(meta["roles"]) if r) or "—"
            pr_cell = (
                ", ".join(f"#{p}" for p in sorted(meta["prs"])) if meta["prs"] else "—"
            )
            signals = []
            if meta["built"]:
                signals.append("built")
            if meta["approved"]:
                signals.append("approved")
            if meta["merged"]:
                signals.append("merged")
            if meta["screenshotish"]:
                signals.append("screenshots")
            if not signals and meta["ok"]:
                signals.append("ok")
            sig = ", ".join(signals) if signals else "—"
            lines.append(f"| #{issue} | {roles} | {pr_cell} | {sig} |")
        orphan_prs = sorted(prs - {p for meta in work.values() for p in meta["prs"]})
        if orphan_prs:
            lines.append("")
            lines.append(
                "PRs without an issue field: "
                + ", ".join(f"#{p}" for p in orphan_prs)
            )

    lines.extend(["", "### Features / work completed", ""])
    if not features_done:
        lines.append(
            "_No builder / approve / merge successes in this window "
            "(agents may still have planned or queued work)._"
        )
    else:
        lines.append(
            "Issues where agents built, approved, or merged work "
            "(plain-language from trace actions):"
        )
        lines.append("")
        for issue in features_done:
            meta = work[issue]
            bits = []
            if meta["built"]:
                bits.append("implemented by Builder")
            if meta["approved"]:
                bits.append("approved by Reviewer")
            if meta["merged"]:
                bits.append("merged by Gate")
            # Unique human labels from actions
            labels = sorted(
                {
                    _action_label(a)
                    for a in meta["actions"]
                    if a
                    and not a.startswith("permission")
                    and a != "weekly_digest"
                }
            )
            detail = "; ".join(bits) if bits else ", ".join(labels[:4])
            lines.append(f"- **#{issue}** — {detail}")

    lines.extend(["", "### Screenshots & visual evidence", ""])
    lines.append(
        "Pre-merge Reviewer captures **PR branch** only (local uvicorn, "
        "`ADMIN_PREVIEW_MODE` for `/admin`); this is the only screenshot "
        "evidence — post-deploy CI records `/health` JSON only, no "
        "screenshots ([docs/SCREENSHOTS.md](../docs/SCREENSHOTS.md))."
    )
    lines.append("")
    if not screenshot_issues:
        lines.append(
            "_No review/merge actions in this window that imply screenshot "
            "evidence was posted._"
        )
    else:
        lines.append("| Issue | Evidence (from agent actions) |")
        lines.append("| ---: | --- |")
        for issue in screenshot_issues:
            meta = work[issue]
            parts = []
            if meta["approved"] or any(
                a.startswith("review:") for a in meta["actions"]
            ):
                parts.append("pre-merge screenshots (PR branch)")
            if meta["merged"]:
                parts.append("post-deploy health record (no screenshots)")
            if not parts:
                parts.append("review / visual gate activity")
            lines.append(f"| #{issue} | {'; '.join(parts)} |")

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
