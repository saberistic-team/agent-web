#!/usr/bin/env python3
"""Unit tests for Cursor SDK codegen helpers (no live API)."""

from __future__ import annotations

from codegen_cursor import _commit_subject, _pr_number_from_url, build_prompt


def test_pr_number_from_url() -> None:
    assert (
        _pr_number_from_url("https://github.com/org/repo/pull/42") == 42
    )
    assert _pr_number_from_url("") is None


def test_build_prompt_includes_issue(tmp_path) -> None:
    brief = tmp_path / "builder.md"
    brief.write_text("Be minimal.\n", encoding="utf-8")
    text = build_prompt(
        repo="saberistic-team/agent-web",
        issue=42,
        title="User flow",
        body="Parent: #41\nDo the flow.",
        brief=brief,
        runtime="local",
    )
    assert "#42" in text
    assert "do NOT git commit" in text
    assert "User flow" in text
    assert "Be minimal." in text


def test_commit_subject_uses_agent_summary() -> None:
    subject = _commit_subject(
        242, "Add pricing page", "Added a new pricing page with three tiers.\n\nMore details..."
    )
    assert subject == "builder(#242): Added a new pricing page with three tiers."


def test_commit_subject_strips_filler_prefix() -> None:
    subject = _commit_subject(242, "Add pricing page", "Summary: added the pricing page.")
    assert subject == "builder(#242): added the pricing page."


def test_commit_subject_falls_back_to_title() -> None:
    subject = _commit_subject(242, "Add pricing page", "")
    assert subject == "builder(#242): Add pricing page"


def test_commit_subject_truncates_long_lines() -> None:
    long_summary = "x" * 100
    subject = _commit_subject(242, "Add pricing page", long_summary)
    assert subject.startswith("builder(#242): ")
    assert len(subject) <= 72
    assert subject.endswith("…")


def test_cursor_runtime_defaults_local(monkeypatch) -> None:
    from codegen_cursor import cursor_runtime

    monkeypatch.delenv("CURSOR_RUNTIME", raising=False)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert cursor_runtime() == "local"
    monkeypatch.setenv("CURSOR_RUNTIME", "cloud")
    assert cursor_runtime() == "cloud"
