#!/usr/bin/env python3
"""Unit tests for Cursor SDK codegen helpers (no live API)."""

from __future__ import annotations

from codegen_cursor import _pr_number_from_url, build_prompt


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
    )
    assert "#42" in text
    assert "Closes #42" in text
    assert "User flow" in text
    assert "Be minimal." in text
