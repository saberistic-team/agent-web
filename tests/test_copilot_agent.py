from __future__ import annotations

from pathlib import Path

from copilot_agent import build_custom_instructions, copilot_enabled


def test_copilot_enabled_requires_token(monkeypatch) -> None:
    monkeypatch.delenv("COPILOT_TOKEN", raising=False)
    monkeypatch.delenv("COPILOT_ASSIGN_TOKEN", raising=False)
    monkeypatch.delenv("CODEGEN_PROVIDER", raising=False)
    monkeypatch.delenv("COPILOT_DISABLED", raising=False)
    assert copilot_enabled() is False
    monkeypatch.setenv("COPILOT_TOKEN", "ghp_test")
    assert copilot_enabled() is True


def test_custom_instructions_include_issue(tmp_path: Path) -> None:
    brief = tmp_path / "builder.md"
    brief.write_text("# Builder\nDo the thing.\n", encoding="utf-8")
    text = build_custom_instructions(
        title="About page",
        body="Add about.html",
        brief=brief,
        ui=True,
    )
    assert "About page" in text
    assert "Add about.html" in text
    assert "brutal-minimalist" in text.lower() or "Brutal-minimalist" in text
