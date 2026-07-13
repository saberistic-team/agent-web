#!/usr/bin/env python3
"""Unit tests for Builder conflict-resolution helpers."""

from __future__ import annotations

from builder_conflicts import (
    default_resolve_file,
    format_conflict_resolution_brief,
    pr_needs_conflict_resolution,
    strip_conflict_markers_prefer_head,
    summarize_recent_closed_work,
)


def test_pr_needs_conflict_resolution_dirty() -> None:
    assert pr_needs_conflict_resolution(
        {"mergeable": False, "mergeable_state": "dirty", "state": "open"}
    )
    assert pr_needs_conflict_resolution(
        {"mergeable": True, "mergeable_state": "dirty", "state": "open"}
    )
    assert not pr_needs_conflict_resolution(
        {"mergeable": True, "mergeable_state": "clean", "state": "open"}
    )
    assert not pr_needs_conflict_resolution(
        {"mergeable": True, "mergeable_state": "clean", "merged": True}
    )


def test_strip_conflict_markers_prefer_head() -> None:
    text = (
        "line1\n"
        "<<<<<<< HEAD\n"
        "ours\n"
        "=======\n"
        "theirs\n"
        ">>>>>>> main\n"
        "line2\n"
    )
    assert strip_conflict_markers_prefer_head(text) == "line1\nours\nline2\n"


def test_default_resolve_file_uses_chat_when_clean(monkeypatch) -> None:
    conflicted = (
        "<<<<<<< HEAD\nfeature\n=======\nseo\n>>>>>>> main\n"
    )

    def chat(system: str, user: str) -> str:
        assert "Conflicted file" in user
        return "feature\nseo\n"

    assert default_resolve_file("site/index.html", conflicted, "brief", chat=chat) == (
        "feature\nseo"
    )


def test_default_resolve_file_falls_back_when_chat_returns_markers() -> None:
    conflicted = "<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> main\n"

    def chat(system: str, user: str) -> str:
        return conflicted

    assert default_resolve_file("app/main.py", conflicted, "brief", chat=chat) == "ours\n"


def test_summarize_recent_closed_work(monkeypatch) -> None:
    monkeypatch.setattr(
        "builder_conflicts.list_recent_merged_prs",
        lambda repo, limit=8: [
            {
                "number": 73,
                "title": "Repair technical SEO (#68)",
                "body": "Closes #68\n\nCanonical + robots.",
                "merged_at": "2026-07-13T00:00:00Z",
            }
        ],
    )
    monkeypatch.setattr(
        "builder_conflicts.pr_changed_paths",
        lambda repo, pr: ["app/seo.py", "site/index.html"],
    )
    monkeypatch.setattr(
        "builder_conflicts.list_recent_closed_issues",
        lambda repo, limit=8: [
            {"number": 68, "title": "Repair technical SEO", "body": "## Goal\nFix SEO."}
        ],
    )
    text = summarize_recent_closed_work("saberistic-team/agent-web")
    assert "#73" in text
    assert "`app/seo.py`" in text
    assert "#68" in text
    assert "Recently closed issues" in text


def test_format_conflict_resolution_brief(monkeypatch) -> None:
    monkeypatch.setattr(
        "builder_conflicts.summarize_recent_closed_work",
        lambda repo, **kwargs: "## Recently merged PRs\n- #73: SEO",
    )
    pr = {
        "number": 76,
        "title": "builder: OG (#67)",
        "body": "Closes #67",
        "head": {"ref": "builder/67-p1"},
        "base": {"ref": "main"},
    }
    brief = format_conflict_resolution_brief(
        "saberistic-team/agent-web",
        67,
        pr,
        conflicted_paths=["site/index.html"],
    )
    assert "issue #67" in brief
    assert "PR #76" in brief
    assert "`site/index.html`" in brief
    assert "#73" in brief


def test_maybe_resolve_pr_conflicts_skips_clean(monkeypatch) -> None:
    from builder_conflicts import maybe_resolve_pr_conflicts

    monkeypatch.setattr(
        "builder_conflicts.linked_open_prs",
        lambda repo, issue: [{"number": 76, "title": "x (#67)", "body": "Closes #67"}],
    )
    monkeypatch.setattr(
        "builder_conflicts.refresh_pr",
        lambda repo, n: {
            "number": 76,
            "mergeable": True,
            "mergeable_state": "clean",
            "state": "open",
            "head": {"ref": "builder/67-p1"},
            "base": {"ref": "main"},
        },
    )
    result = maybe_resolve_pr_conflicts("saberistic-team/agent-web", 67)
    assert result["status"] == "clean"
    assert result["pr"] == 76
