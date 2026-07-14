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


def priority_labels_on_issue(labels: set[str] | list[str] | None) -> list[str]:
    """Return all priority:* labels on an issue (may be empty)."""
    return sorted(label for label in labels or [] if label.startswith("priority:"))


def has_duplicate_priority_labels(labels: set[str] | list[str] | None) -> bool:
    """True when more than one priority:* label is present."""
    return len(priority_labels_on_issue(labels)) > 1


def priority_from_labels(labels: set[str] | list[str] | None) -> str | None:
    """Return the highest-urgency canonical priority:* label if present."""
    canonical = [label for label in labels or [] if label in PRIORITY_RANK]
    if not canonical:
        return None
    return min(canonical, key=lambda label: PRIORITY_RANK[label])


def resolve_priority_label(
    title: str,
    body: str = "",
    labels: set[str] | list[str] | None = None,
) -> str:
    """Resolve one canonical priority label (dedupes by highest urgency)."""
    canonical = [label for label in labels or [] if label in PRIORITY_RANK]
    if canonical:
        return min(canonical, key=lambda label: PRIORITY_RANK[label])
    if priority_labels_on_issue(labels):
        return infer_priority_label(title, body, labels=None)
    return infer_priority_label(title, body, labels)


def infer_priority_label(
    title: str,
    body: str = "",
    labels: set[str] | list[str] | None = None,
) -> str:
    """Infer priority from issue text when no canonical label is present."""
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
