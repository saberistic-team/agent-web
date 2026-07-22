"""Dispatcher priority normalization (mocked GitHub API)."""

from __future__ import annotations

import pytest

from dispatch_queue import (
    ensure_priority,
    list_awaiting_dispatch,
    replace_priority_label,
)


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


@pytest.mark.unit
def test_list_awaiting_dispatch_filters_closed_milestone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "dispatch_queue.search_issues",
        lambda repo, query: [
            {
                "number": 101,
                "labels": [
                    {"name": "status:queued"},
                    {"name": "type:feature"},
                    {"name": "priority:normal"},
                ],
                "milestone": {"number": 1, "title": "Current", "state": "open"},
            },
            {
                "number": 102,
                "labels": [
                    {"name": "status:queued"},
                    {"name": "type:feature"},
                    {"name": "priority:high"},
                ],
                "milestone": {"number": 9, "title": "Old", "state": "closed"},
            },
            {
                "number": 103,
                "labels": [
                    {"name": "status:queued"},
                    {"name": "type:bug"},
                    {"name": "priority:critical"},
                ],
                "milestone": None,
            },
        ],
    )
    monkeypatch.setattr(
        "dispatch_queue.list_open_milestones",
        lambda repo: [{"number": 1, "title": "Current", "state": "open"}],
    )

    awaiting, skipped = list_awaiting_dispatch("o/r")

    assert [i["number"] for i in awaiting] == [103, 101]
    assert len(skipped) == 1
    assert skipped[0]["issue"] == 102
    assert skipped[0]["reason"] == "milestone_not_open"


@pytest.mark.unit
def test_list_awaiting_dispatch_orders_by_earliest_due(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "dispatch_queue.search_issues",
        lambda repo, query: [
            {
                "number": 201,
                "labels": [
                    {"name": "status:queued"},
                    {"name": "type:feature"},
                    {"name": "priority:high"},
                ],
                "milestone": {"number": 2, "title": "Later"},
            },
            {
                "number": 202,
                "labels": [
                    {"name": "status:queued"},
                    {"name": "type:feature"},
                    {"name": "priority:normal"},
                ],
                "milestone": {"number": 1, "title": "Soon"},
            },
        ],
    )
    monkeypatch.setattr(
        "dispatch_queue.list_open_milestones",
        lambda repo: [
            {
                "number": 1,
                "title": "Soon",
                "state": "open",
                "due_on": "2026-08-01T00:00:00Z",
            },
            {
                "number": 2,
                "title": "Later",
                "state": "open",
                "due_on": "2026-12-01T00:00:00Z",
            },
        ],
    )

    awaiting, skipped = list_awaiting_dispatch("o/r")

    assert [i["number"] for i in awaiting] == [202, 201]
    assert skipped == []


@pytest.mark.unit
def test_dispatch_next_skips_open_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dispatch_queue import dispatch_next

    monkeypatch.setattr(
        "dispatch_queue.list_awaiting_dispatch",
        lambda repo: (
            [
                {
                    "number": 204,
                    "body": "Depends on: #199\n",
                    "labels": [
                        {"name": "status:queued"},
                        {"name": "type:feature"},
                        {"name": "priority:low"},
                    ],
                    "milestone": {"title": "WorldGraph"},
                },
                {
                    "number": 210,
                    "body": "## Dependencies\nNone\n",
                    "labels": [
                        {"name": "status:queued"},
                        {"name": "type:feature"},
                        {"name": "priority:normal"},
                    ],
                    "milestone": {"title": "WorldGraph"},
                },
            ],
            [],
        ),
    )
    monkeypatch.setattr("dispatch_queue.agent_in_progress", lambda *_a, **_k: False)
    monkeypatch.setattr(
        "dispatch_queue.reconcile_issue_dependencies",
        lambda repo, number, body="", write=True: {
            "issue": number,
            "ok": True,
            "blockers": [{"number": 199}] if number == 204 else [],
            "added_blocked_by": (
                [{"blocking": 199, "status": "ok"}] if number == 204 else []
            ),
            "added_sub_issues": [],
            "body_updated": number == 204,
            "body": body,
        },
    )
    monkeypatch.setattr(
        "dispatch_queue.reconcile_comment",
        lambda summary: (
            "### dependency_reconcile\n- added_blocked_by: #199\n"
            if summary.get("added_blocked_by")
            else None
        ),
    )
    monkeypatch.setattr(
        "dispatch_queue.dependency_block_reason",
        lambda repo, number, body="", reconcile=False: (
            f"#{number} is blocked by open dependencies: #199"
            if number == 204
            else None
        ),
    )
    monkeypatch.setattr("dispatch_queue._recent_dispatcher_skip", lambda *_a, **_k: False)
    comments: list[tuple[int, str]] = []
    monkeypatch.setattr(
        "dispatch_queue.post_issue_comment",
        lambda repo, number, body: comments.append((number, body)),
    )
    monkeypatch.setattr("dispatch_queue.ensure_priority", lambda *_a, **_k: "priority:normal")
    monkeypatch.setattr("dispatch_queue.delete_label", lambda *_a, **_k: None)
    monkeypatch.setattr("dispatch_queue.add_labels", lambda *_a, **_k: None)

    result = dispatch_next("o/r", dry_run=False)

    assert result["skipped_deps"][0]["issue"] == 204
    assert result["dispatched"] == [
        {
            "issue": 210,
            "agent": "agent:builder",
            "priority": "priority:normal",
            "milestone": "WorldGraph",
        }
    ]
    assert any("### dependency_reconcile" in body for _, body in comments)
    assert any("### dispatcher_skip" in body for number, body in comments if number == 204)
    assert any("### dispatcher_dispatch" in body for _, body in comments)
