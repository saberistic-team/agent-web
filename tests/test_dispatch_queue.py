"""Dispatcher priority guard tests (mocked GitHub API)."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from dispatch_queue import ensure_priority, replace_priority_labels


@pytest.mark.unit
def test_ensure_priority_keeps_existing_medium_without_adding_normal() -> None:
    issue = {
        "number": 86,
        "title": "Analytics semantics",
        "body": "",
        "labels": [{"name": "priority:medium"}, {"name": "status:queued"}],
    }
    with patch("dispatch_queue.replace_priority_labels") as replace:
        with patch("dispatch_queue.post_issue_comment") as comment:
            priority = ensure_priority("o/r", issue)
    assert priority == "priority:medium"
    replace.assert_not_called()
    comment.assert_not_called()


@pytest.mark.unit
def test_ensure_priority_normalizes_medium_and_normal_duplicates() -> None:
    issue = {
        "number": 87,
        "title": "Conversion paths",
        "body": "",
        "labels": [
            {"name": "priority:medium"},
            {"name": "priority:normal"},
            {"name": "status:queued"},
        ],
    }
    deleted: list[str] = []
    added: list[list[str]] = []

    def fake_delete(repo: str, number: int, label: str) -> None:
        deleted.append(label)

    def fake_add(repo: str, number: int, labels: list[str]) -> None:
        added.append(list(labels))

    with patch("dispatch_queue.delete_label", side_effect=fake_delete):
        with patch("dispatch_queue.add_labels", side_effect=fake_add):
            with patch("dispatch_queue.post_issue_comment") as comment:
                priority = ensure_priority("o/r", issue)

    assert priority == "priority:medium"
    assert set(deleted) == {"priority:medium", "priority:normal"}
    assert added == [["priority:medium"]]
    comment.assert_called_once()
    assert "dispatcher_priority_normalized" in comment.call_args[0][2]


@pytest.mark.unit
def test_ensure_priority_infers_and_replaces_when_missing() -> None:
    issue = {
        "number": 12,
        "title": "Routine cleanup",
        "body": "",
        "labels": [{"name": "status:queued"}],
    }
    with patch("dispatch_queue.replace_priority_labels") as replace:
        priority = ensure_priority("o/r", issue)
    assert priority == "priority:normal"
    replace.assert_called_once_with("o/r", 12, {"status:queued"}, "priority:normal")


@pytest.mark.unit
def test_replace_priority_labels_clears_axis_before_set() -> None:
    deleted: list[str] = []
    added: list[list[str]] = []

    with patch("dispatch_queue.delete_label", side_effect=lambda *a: deleted.append(a[2])):
        with patch(
            "dispatch_queue.add_labels",
            side_effect=lambda *a: added.append(list(a[2])),
        ):
            replace_priority_labels(
                "o/r",
                5,
                {"priority:high", "priority:low", "type:bug"},
                "priority:high",
            )

    assert set(deleted) == {"priority:high", "priority:low"}
    assert added == [["priority:high"]]
