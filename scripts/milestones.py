#!/usr/bin/env python3
"""Open-milestone eligibility for the agent priority queue.

Humans open/close GitHub milestones to mark the current product phase.
Planner assigns queued work to an open milestone; the dispatcher only
dequeues that phase (plus ``priority:critical`` hotfixes).
"""

from __future__ import annotations

from typing import Any

from github_api import api, split_repo
from priority import (
    DEFAULT_PRIORITY,
    PRIORITY_RANK,
    priority_from_labels,
)


def list_open_milestones(repo: str) -> list[dict[str, Any]]:
    """Return open milestones for the repo (paginated)."""
    owner, name = split_repo(repo)
    results: list[dict[str, Any]] = []
    page = 1
    while page <= 10:
        batch = (
            api(
                "GET",
                f"/repos/{owner}/{name}/milestones"
                f"?state=open&per_page=100&page={page}&sort=due_on&direction=asc",
            )
            or []
        )
        results.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return results


def open_milestone_numbers(milestones: list[dict[str, Any]] | None) -> set[int]:
    """Milestone numbers that are currently open."""
    return {int(m["number"]) for m in milestones or [] if m.get("number") is not None}


def issue_milestone_number(issue: dict[str, Any]) -> int | None:
    """Return the issue's milestone number, or None if unset."""
    milestone = issue.get("milestone")
    if not milestone or milestone.get("number") is None:
        return None
    return int(milestone["number"])


def milestone_due_sort_key(
    milestone: dict[str, Any] | None,
) -> tuple[int, str, int]:
    """Sort key: earliest ``due_on`` first, null due last, then lowest number."""
    if not milestone:
        # No milestone sorts after dated phases (critical handled separately).
        return (2, "", 10**9)
    due = milestone.get("due_on") or ""
    has_due = 0 if due else 1
    return (has_due, due, int(milestone.get("number") or 0))


def pick_current_milestone(
    milestones: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Choose the preferred open milestone for new work.

    Prefer earliest ``due_on`` (nulls last), then lowest milestone number.
    """
    if not milestones:
        return None
    return sorted(milestones, key=milestone_due_sort_key)[0]


def dispatch_sort_key(
    issue: dict[str, Any],
    labels: set[str] | list[str] | None,
    *,
    open_milestones_by_number: dict[int, dict[str, Any]] | None = None,
) -> tuple[Any, ...]:
    """Dispatcher order: critical, then earliest milestone due, then priority.

    1. ``priority:critical`` hotfixes first
    2. earliest milestone ``due_on`` (open milestone catalog preferred)
    3. ``priority:*`` rank
    4. older issue number
    """
    critical = 0 if priority_from_labels(labels) == "priority:critical" else 1
    milestone = issue.get("milestone")
    number = issue_milestone_number(issue)
    if (
        number is not None
        and open_milestones_by_number
        and number in open_milestones_by_number
    ):
        milestone = open_milestones_by_number[number]
    due_key = milestone_due_sort_key(milestone)
    priority = priority_from_labels(labels) or DEFAULT_PRIORITY
    priority_rank = PRIORITY_RANK.get(priority, PRIORITY_RANK[DEFAULT_PRIORITY])
    return (critical, *due_key, priority_rank, int(issue.get("number") or 0))


def is_dispatch_eligible(
    issue: dict[str, Any],
    labels: set[str] | list[str] | None,
    open_numbers: set[int],
) -> bool:
    """True when the dispatcher may start this queued issue.

    - ``priority:critical`` always eligible (hotfix escape hatch)
    - if the repo has no open milestones, all queued work is eligible
    - otherwise the issue must be on an open milestone
    """
    if priority_from_labels(labels) == "priority:critical":
        return True
    if not open_numbers:
        return True
    number = issue_milestone_number(issue)
    return number is not None and number in open_numbers


def assign_milestone(repo: str, issue_number: int, milestone_number: int) -> None:
    """Set the issue milestone (GitHub Issues API)."""
    owner, name = split_repo(repo)
    api(
        "PATCH",
        f"/repos/{owner}/{name}/issues/{issue_number}",
        body={"milestone": milestone_number},
    )


def ensure_open_milestone(
    repo: str,
    issue_number: int,
    issue: dict[str, Any],
    *,
    labels: set[str] | list[str] | None = None,
    open_milestones: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Put non-critical work on an open milestone when missing or closed.

    Returns the milestone object that should apply (existing open or newly
    assigned). Leaves critical issues and empty milestone lists unchanged.
    """
    label_names = set(labels or [])
    if not label_names and issue.get("labels"):
        label_names = {label["name"] for label in issue["labels"]}

    if priority_from_labels(label_names) == "priority:critical":
        return issue.get("milestone")

    milestones = (
        open_milestones
        if open_milestones is not None
        else list_open_milestones(repo)
    )
    open_numbers = open_milestone_numbers(milestones)
    if not open_numbers:
        return issue.get("milestone")

    current_number = issue_milestone_number(issue)
    if current_number is not None and current_number in open_numbers:
        return issue.get("milestone")

    # Prefer parent's open milestone object when present in the list; else pick.
    target = None
    if current_number is not None:
        target = next(
            (m for m in milestones if int(m.get("number") or 0) == current_number),
            None,
        )
    if target is None:
        target = pick_current_milestone(milestones)
    if target is None:
        return issue.get("milestone")

    assign_milestone(repo, issue_number, int(target["number"]))
    return target
