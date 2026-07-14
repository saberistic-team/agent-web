"""Priority inference and queue ordering (no GitHub API)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from priority import (
    DEFAULT_PRIORITY,
    has_duplicate_priority_labels,
    infer_priority_label,
    intended_agent_label,
    is_awaiting_dispatch,
    priority_from_labels,
    priority_labels_on_issue,
    priority_sort_key,
    resolve_priority_label,
)


def test_infer_priority_defaults_to_normal() -> None:
    assert infer_priority_label("Add hello endpoint", "") == DEFAULT_PRIORITY


def test_infer_priority_critical_from_text() -> None:
    assert infer_priority_label("P0 outage on /health", "") == "priority:critical"
    assert (
        infer_priority_label("Fix login", "This is urgent / a blocker.")
        == "priority:critical"
    )


def test_infer_priority_high_and_low() -> None:
    assert infer_priority_label("P1 billing regression", "") == "priority:high"
    assert (
        infer_priority_label("Docs polish", "Nice-to-have when free.")
        == "priority:low"
    )


def test_infer_priority_respects_existing_label() -> None:
    assert (
        infer_priority_label(
            "P0 something",
            "",
            labels={"priority:low", "status:new"},
        )
        == "priority:low"
    )


def test_priority_sort_key_orders_critical_before_low() -> None:
    high = priority_sort_key({"priority:critical"}, 99)
    low = priority_sort_key({"priority:low"}, 1)
    normal = priority_sort_key({"priority:normal"}, 50)
    assert high < normal < low


def test_priority_sort_key_fifo_within_same_priority() -> None:
    older = priority_sort_key({"priority:high"}, 10)
    newer = priority_sort_key({"priority:high"}, 20)
    assert older < newer


def test_intended_agent_from_type() -> None:
    assert intended_agent_label({"type:docs", "status:queued"}) == "agent:docs"
    assert intended_agent_label({"type:feature", "status:queued"}) == "agent:builder"


def test_is_awaiting_dispatch() -> None:
    assert is_awaiting_dispatch(
        {"status:queued", "type:bug", "priority:high"}
    )
    assert not is_awaiting_dispatch(
        {"status:queued", "type:bug", "agent:builder"}
    )
    assert not is_awaiting_dispatch({"status:in-progress", "agent:builder"})


def test_priority_from_labels_prefers_highest_urgency_when_duplicates() -> None:
    labels = {"priority:normal", "priority:medium", "status:done"}
    assert priority_from_labels(labels) == "priority:medium"


def test_resolve_priority_label_keeps_medium_over_normal() -> None:
    labels = {"priority:normal", "priority:medium"}
    assert (
        resolve_priority_label("Issue title", "", labels) == "priority:medium"
    )


def test_has_duplicate_priority_labels() -> None:
    assert has_duplicate_priority_labels({"priority:medium", "priority:normal"})
    assert not has_duplicate_priority_labels({"priority:medium", "status:done"})


def test_priority_labels_on_issue_sorted() -> None:
    assert priority_labels_on_issue(
        {"priority:normal", "priority:medium", "status:queued"}
    ) == ["priority:medium", "priority:normal"]


def test_priority_sort_key_orders_medium_between_high_and_normal() -> None:
    high = priority_sort_key({"priority:high"}, 1)
    medium = priority_sort_key({"priority:medium"}, 1)
    normal = priority_sort_key({"priority:normal"}, 1)
    assert high < medium < normal
