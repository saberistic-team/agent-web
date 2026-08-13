"""Unit tests for PR label mirroring helpers."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from pr_labels import (
    apply_pr_mirror,
    apply_to_linked_prs,
    desired_pr_labels,
    mirror_pr_milestone,
)


@pytest.mark.unit
def test_desired_pr_labels_copies_type_and_priority() -> None:
    assert desired_pr_labels(
        {"status:queued", "type:feature", "priority:high", "agent:builder"},
        default_review="review:needs-review",
    ) == ["type:feature", "priority:high", "review:needs-review"]


@pytest.mark.unit
def test_desired_pr_labels_never_includes_agent_or_status() -> None:
    labels = desired_pr_labels(
        {
            "status:needs-review",
            "agent:reviewer",
            "type:bug",
            "priority:normal",
            "review:needs-review",
        }
    )
    assert labels == ["type:bug", "priority:normal", "review:needs-review"]
    assert not any(l.startswith(("agent:", "status:")) for l in labels)


@pytest.mark.unit
def test_desired_pr_labels_review_override() -> None:
    assert desired_pr_labels(
        {"type:docs", "priority:low", "review:needs-review"},
        review="review:approved",
    ) == ["type:docs", "priority:low", "review:approved"]


@pytest.mark.unit
def test_desired_pr_labels_docs_gets_needs_review_by_default() -> None:
    assert desired_pr_labels(
        {"type:docs", "priority:normal"},
        default_review="review:needs-review",
    ) == ["type:docs", "priority:normal", "review:needs-review"]


@pytest.mark.unit
def test_desired_pr_labels_omits_review_when_default_none() -> None:
    assert desired_pr_labels(
        {"type:docs", "priority:normal"},
        default_review=None,
    ) == ["type:docs", "priority:normal"]


@pytest.mark.unit
def test_desired_pr_labels_rejects_invalid_review() -> None:
    with pytest.raises(ValueError, match="invalid review"):
        desired_pr_labels({"type:bug"}, review="review:nope")


@pytest.mark.unit
def test_apply_pr_mirror_clears_and_sets(monkeypatch: pytest.MonkeyPatch) -> None:
    deleted: list[str] = []
    added: list[list[str]] = []
    milestones: list[tuple[int, int]] = []

    def fake_get_issue(repo: str, number: int) -> dict:
        if number == 10:
            return {
                "labels": [
                    {"name": "type:feature"},
                    {"name": "priority:high"},
                    {"name": "status:needs-review"},
                ],
                "milestone": {"number": 3, "title": "Phase"},
            }
        return {
            "labels": [
                {"name": "type:old"},
                {"name": "priority:low"},
                {"name": "review:approved"},
                {"name": "agent:builder"},
            ],
            "milestone": None,
        }

    monkeypatch.setattr("pr_labels.get_issue", fake_get_issue)
    monkeypatch.setattr(
        "pr_labels.delete_label",
        lambda repo, number, label: deleted.append(label),
    )
    monkeypatch.setattr(
        "pr_labels.add_labels",
        lambda repo, number, labels: added.append(list(labels)),
    )
    monkeypatch.setattr(
        "pr_labels.assign_milestone",
        lambda repo, number, milestone: milestones.append((number, milestone)),
    )

    result = apply_pr_mirror(
        "o/r",
        10,
        55,
        default_review="review:needs-review",
    )
    assert result == ["type:feature", "priority:high", "review:needs-review"]
    assert set(deleted) == {"type:old", "priority:low", "review:approved"}
    assert "agent:builder" not in deleted  # left alone (should not be on PR anyway)
    assert added == [["type:feature", "priority:high", "review:needs-review"]]
    assert milestones == [(55, 3)]


@pytest.mark.unit
def test_apply_pr_mirror_skips_milestone_when_issue_has_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    milestones: list[tuple[int, int]] = []

    monkeypatch.setattr(
        "pr_labels.get_issue",
        lambda repo, number: {
            "labels": [{"name": "type:bug"}, {"name": "priority:critical"}],
            "milestone": None,
        },
    )
    monkeypatch.setattr("pr_labels.clear_pr_mirror_labels", lambda *a, **k: None)
    monkeypatch.setattr("pr_labels.add_labels", lambda *a, **k: None)
    monkeypatch.setattr(
        "pr_labels.assign_milestone",
        lambda repo, number, milestone: milestones.append((number, milestone)),
    )

    apply_pr_mirror("o/r", 11, 56, default_review="review:needs-review")
    assert milestones == []


@pytest.mark.unit
def test_mirror_pr_milestone_copies_from_issue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    milestones: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "pr_labels.get_issue",
        lambda repo, number: {"milestone": {"number": 7, "title": "Ship"}},
    )
    monkeypatch.setattr(
        "pr_labels.assign_milestone",
        lambda repo, number, milestone: milestones.append((number, milestone)),
    )
    assert mirror_pr_milestone("o/r", 12, 90) == 7
    assert milestones == [(90, 7)]


@pytest.mark.unit
def test_apply_to_linked_prs_uses_unique_open_pr(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, str | None]] = []

    monkeypatch.setattr(
        "pr_labels.unique_open_pr_or_none",
        lambda repo, issue: {"number": 71},
    )

    def fake_apply(repo, issue, pr, *, review=None, default_review=None):
        calls.append((pr, review))
        return [f"pr-{pr}"]

    monkeypatch.setattr("pr_labels.apply_pr_mirror", fake_apply)

    out = apply_to_linked_prs("o/r", 9, review="review:changes-requested")
    assert out == {71: ["pr-71"]}
    assert calls == [(71, "review:changes-requested")]


@pytest.mark.unit
def test_apply_to_linked_prs_explicit_pr(monkeypatch: pytest.MonkeyPatch) -> None:
    with patch("pr_labels.unique_open_pr_or_none") as linked:
        with patch(
            "pr_labels.apply_pr_mirror",
            return_value=["review:approved"],
        ) as apply:
            out = apply_to_linked_prs(
                "o/r",
                9,
                pr=88,
                review="review:approved",
            )
    linked.assert_not_called()
    apply.assert_called_once_with(
        "o/r",
        9,
        88,
        review="review:approved",
        default_review=None,
    )
    assert out == {88: ["review:approved"]}
