#!/usr/bin/env python3
"""Priority axis helpers for label-driven agent queue ordering."""

from __future__ import annotations

import re

PRIORITY_LABELS = (
    "priority:critical",
    "priority:high",
    "priority:medium",
    "priority:normal",
    "priority:low",
)

PRIORITY_RANK = {label: index for index, label in enumerate(PRIORITY_LABELS)}
DEFAULT_PRIORITY = "priority:normal"


def all_priority_labels(labels: set[str] | list[str] | None) -> list[str]:
    """Return every priority:* label on the issue, sorted for stable comparison."""
    return sorted(label for label in (labels or []) if label.startswith("priority:"))


def resolve_priority_label(priority_labels: list[str]) -> str | None:
    """Pick one priority when duplicates exist; None if intent stays ambiguous."""
    if len(priority_labels) <= 1:
        return priority_labels[0] if priority_labels else None

    non_default = [label for label in priority_labels if label != DEFAULT_PRIORITY]
    if len(non_default) == 1:
        return non_default[0]
    if not non_default:
        return DEFAULT_PRIORITY

    ranked = sorted(
        non_default,
        key=lambda label: PRIORITY_RANK.get(label, len(PRIORITY_RANK)),
    )
    best = ranked[0]
    second = ranked[1]
    if PRIORITY_RANK.get(best, len(PRIORITY_RANK)) == PRIORITY_RANK.get(
        second, len(PRIORITY_RANK)
    ):
        return None
    return best


def has_duplicate_priority_labels(labels: set[str] | list[str] | None) -> bool:
    """True when more than one priority:* label is present."""
    return len(all_priority_labels(labels)) > 1


def priority_from_labels(labels: set[str] | list[str] | None) -> str | None:
    """Return the resolved priority:* label, or None when absent/ambiguous."""
    found = all_priority_labels(labels)
    if not found:
        return None
    if len(found) == 1:
        return found[0]
    return resolve_priority_label(found)


def infer_priority_label(
    title: str,
    body: str = "",
    labels: set[str] | list[str] | None = None,
) -> str:
    """Resolve priority from an existing label, else infer from issue text."""
    existing = priority_from_labels(labels)
    if existing:
        return existing

    text = f"{title}\n{body}".lower()
    if re.search(
        r"\b(priority:\s*critical|p0|sev(?:erity)?\s*[-:]?\s*0|critical|urgent|"
        r"blocker|asap)\b",
        text,
    ):
        return "priority:critical"
    if re.search(
        r"\b(priority:\s*high|p1|sev(?:erity)?\s*[-:]?\s*1|high[\s-]?priority|"
        r"important)\b",
        text,
    ):
        return "priority:high"
    if re.search(
        r"\b(priority:\s*medium|p2|sev(?:erity)?\s*[-:]?\s*2|medium[\s-]?priority)\b",
        text,
    ):
        return "priority:medium"
    if re.search(
        r"\b(priority:\s*low|p3|sev(?:erity)?\s*[-:]?\s*3|low[\s-]?priority|"
        r"nice[\s-]to[\s-]have|whenever)\b",
        text,
    ):
        return "priority:low"
    return DEFAULT_PRIORITY


def priority_sort_key(
    labels: set[str] | list[str] | None,
    issue_number: int,
) -> tuple[int, int]:
    """Lower tuple sorts first: higher priority, then older issue number."""
    label = priority_from_labels(labels) or DEFAULT_PRIORITY
    return (PRIORITY_RANK.get(label, PRIORITY_RANK[DEFAULT_PRIORITY]), issue_number)


def intended_agent_label(labels: set[str] | list[str] | None) -> str:
    """Pick builder vs docs from type:* (default builder)."""
    label_set = set(labels or [])
    if "type:docs" in label_set:
        return "agent:docs"
    return "agent:builder"


def is_awaiting_dispatch(labels: set[str] | list[str] | None) -> bool:
    """Queued work waiting for the dispatcher (no owning run agent yet)."""
    label_set = set(labels or [])
    if "status:queued" not in label_set:
        return False
    if label_set & {"agent:builder", "agent:docs", "agent:reviewer"}:
        return False
    return True
