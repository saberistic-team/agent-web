"""Open-milestone eligibility helpers (no live GitHub)."""

from __future__ import annotations

import pytest

from milestones import (
    dispatch_sort_key,
    ensure_open_milestone,
    is_dispatch_eligible,
    issue_milestone_number,
    open_milestone_numbers,
    pick_current_milestone,
)


@pytest.mark.unit
def test_pick_current_milestone_prefers_earliest_due() -> None:
    chosen = pick_current_milestone(
        [
            {"number": 3, "title": "Later", "due_on": "2026-12-01T00:00:00Z"},
            {"number": 1, "title": "Soon", "due_on": "2026-08-01T00:00:00Z"},
            {"number": 2, "title": "No due", "due_on": None},
        ]
    )
    assert chosen is not None
    assert chosen["number"] == 1


@pytest.mark.unit
def test_pick_current_milestone_null_due_uses_lowest_number() -> None:
    chosen = pick_current_milestone(
        [
            {"number": 5, "title": "B", "due_on": None},
            {"number": 2, "title": "A", "due_on": None},
        ]
    )
    assert chosen is not None
    assert chosen["number"] == 2


@pytest.mark.unit
def test_is_dispatch_eligible_requires_open_milestone() -> None:
    open_numbers = {1}
    on_open = {
        "number": 10,
        "milestone": {"number": 1, "title": "Current", "state": "open"},
    }
    on_closed = {
        "number": 11,
        "milestone": {"number": 9, "title": "Old", "state": "closed"},
    }
    no_milestone = {"number": 12, "milestone": None}

    assert is_dispatch_eligible(on_open, {"priority:normal"}, open_numbers)
    assert not is_dispatch_eligible(on_closed, {"priority:normal"}, open_numbers)
    assert not is_dispatch_eligible(no_milestone, {"priority:high"}, open_numbers)


@pytest.mark.unit
def test_is_dispatch_eligible_critical_escape_hatch() -> None:
    open_numbers = {1}
    no_milestone = {"number": 12, "milestone": None}
    assert is_dispatch_eligible(
        no_milestone, {"priority:critical", "status:queued"}, open_numbers
    )


@pytest.mark.unit
def test_is_dispatch_eligible_when_no_open_milestones() -> None:
    issue = {"number": 12, "milestone": None}
    assert is_dispatch_eligible(issue, {"priority:normal"}, set())


@pytest.mark.unit
def test_issue_milestone_number_and_open_set() -> None:
    assert issue_milestone_number({"milestone": None}) is None
    assert issue_milestone_number({"milestone": {"number": 4}}) == 4
    assert open_milestone_numbers([{"number": 1}, {"number": 2}]) == {1, 2}


@pytest.mark.unit
def test_ensure_open_milestone_assigns_current(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assigned: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "milestones.assign_milestone",
        lambda repo, number, milestone: assigned.append((number, milestone)),
    )
    open_ms = [
        {"number": 1, "title": "Current", "due_on": "2026-08-01T00:00:00Z"},
    ]
    issue = {"number": 40, "milestone": None, "labels": [{"name": "priority:normal"}]}
    result = ensure_open_milestone(
        "o/r",
        40,
        issue,
        labels={"priority:normal"},
        open_milestones=open_ms,
    )
    assert result is not None
    assert result["number"] == 1
    assert assigned == [(40, 1)]


@pytest.mark.unit
def test_dispatch_sort_key_earliest_due_before_higher_priority() -> None:
    by_number = {
        1: {"number": 1, "title": "Soon", "due_on": "2026-08-01T00:00:00Z"},
        2: {"number": 2, "title": "Later", "due_on": "2026-12-01T00:00:00Z"},
    }
    early_normal = {
        "number": 50,
        "milestone": {"number": 1},
        "labels": [{"name": "priority:normal"}],
    }
    late_high = {
        "number": 40,
        "milestone": {"number": 2},
        "labels": [{"name": "priority:high"}],
    }
    critical = {
        "number": 99,
        "milestone": None,
        "labels": [{"name": "priority:critical"}],
    }
    ordered = sorted(
        [late_high, early_normal, critical],
        key=lambda issue: dispatch_sort_key(
            issue,
            {label["name"] for label in issue["labels"]},
            open_milestones_by_number=by_number,
        ),
    )
    assert [i["number"] for i in ordered] == [99, 50, 40]


@pytest.mark.unit
def test_ensure_open_milestone_skips_critical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assigned: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "milestones.assign_milestone",
        lambda repo, number, milestone: assigned.append((number, milestone)),
    )
    issue = {"number": 41, "milestone": None}
    result = ensure_open_milestone(
        "o/r",
        41,
        issue,
        labels={"priority:critical"},
        open_milestones=[{"number": 1, "title": "Current"}],
    )
    assert result is None
    assert assigned == []
