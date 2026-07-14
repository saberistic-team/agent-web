"""Dispatcher priority normalization (mocked GitHub API)."""

from __future__ import annotations

import pytest

from dispatch_queue import ensure_priority, replace_priority_label


@pytest.mark.unit
def test_replace_priority_label_clears_all_priority_prefixes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deleted: list[str] = []
    added: list[list[str]] = []

    monkeypatch.setattr(
        "dispatch_queue.api",
        lambda method, path: {
            "labels": [
                {"name": "priority:medium"},
                {"name": "priority:normal"},
                {"name": "status:queued"},
            ]
        },
    )
    monkeypatch.setattr(
        "dispatch_queue.delete_label",
        lambda repo, number, label: deleted.append(label),
    )
    monkeypatch.setattr(
        "dispatch_queue.add_labels",
        lambda repo, number, labels: added.append(list(labels)),
    )

    replace_priority_label("o/r", 86, "priority:medium")

    assert set(deleted) == {"priority:medium", "priority:normal"}
    assert added == [["priority:medium"]]


@pytest.mark.unit
def test_ensure_priority_replaces_duplicate_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replaced: list[tuple[int, str]] = []
    comments: list[int] = []

    monkeypatch.setattr(
        "dispatch_queue.replace_priority_label",
        lambda repo, number, label: replaced.append((number, label)),
    )
    monkeypatch.setattr(
        "dispatch_queue.post_issue_comment",
        lambda repo, number, body: comments.append(number),
    )

    issue = {
        "number": 87,
        "title": "Improve conversion paths",
        "body": "",
        "labels": [
            {"name": "priority:medium"},
            {"name": "priority:normal"},
            {"name": "status:queued"},
        ],
    }

    priority = ensure_priority("o/r", issue)

    assert priority == "priority:medium"
    assert replaced == [(87, "priority:medium")]
    assert comments == [87]


@pytest.mark.unit
def test_ensure_priority_leaves_single_canonical_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replaced: list[tuple[int, str]] = []

    monkeypatch.setattr(
        "dispatch_queue.replace_priority_label",
        lambda repo, number, label: replaced.append((number, label)),
    )
    monkeypatch.setattr("dispatch_queue.post_issue_comment", lambda *args: None)

    issue = {
        "number": 86,
        "title": "Analytics semantics",
        "body": "",
        "labels": [{"name": "priority:medium"}, {"name": "status:done"}],
    }

    priority = ensure_priority("o/r", issue)

    assert priority == "priority:medium"
    assert replaced == []
