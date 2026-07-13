"""Unit tests for PR label mirroring helpers."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from pr_labels import (
    apply_pr_mirror,
    apply_to_linked_prs,
    desired_pr_labels,
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
def test_desired_pr_labels_docs_skips_review_by_default() -> None:
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

    monkeypatch.setattr(
        "pr_labels.get_labels",
        lambda repo, number: (
            {"type:feature", "priority:high", "status:needs-review"}
            if number == 10
            else {"type:old", "priority:low", "review:approved", "agent:builder"}
        ),
    )
    monkeypatch.setattr(
        "pr_labels.delete_label",
        lambda repo, number, label: deleted.append(label),
    )
    monkeypatch.setattr(
        "pr_labels.add_labels",
        lambda repo, number, labels: added.append(list(labels)),
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


@pytest.mark.unit
def test_apply_to_linked_prs_uses_open_prs(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, str | None]] = []

    monkeypatch.setattr(
        "pr_labels.linked_open_prs",
        lambda repo, issue: [{"number": 71}, {"number": 72}],
    )

    def fake_apply(repo, issue, pr, *, review=None, default_review=None):
        calls.append((pr, review))
        return [f"pr-{pr}"]

    monkeypatch.setattr("pr_labels.apply_pr_mirror", fake_apply)

    out = apply_to_linked_prs("o/r", 9, review="review:changes-requested")
    assert out == {71: ["pr-71"], 72: ["pr-72"]}
    assert calls == [
        (71, "review:changes-requested"),
        (72, "review:changes-requested"),
    ]


@pytest.mark.unit
def test_apply_to_linked_prs_explicit_pr(monkeypatch: pytest.MonkeyPatch) -> None:
    with patch("pr_labels.linked_open_prs") as linked:
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
