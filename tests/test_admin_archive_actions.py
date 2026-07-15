"""Regression tests for themed archive/restore admin action buttons (#233)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.admin_layout import archive_action_button_class

ADMIN_CSS = Path(__file__).resolve().parents[1] / "site/assets/admin.css"


def _rule_block(css: str, selector_fragment: str) -> str:
    start = css.index(selector_fragment)
    brace_start = css.index("{", start)
    depth = 0
    for index, char in enumerate(css[brace_start:], start=brace_start):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return css[start : index + 1]
    raise AssertionError(f"Unclosed rule for {selector_fragment!r}")


@pytest.mark.unit
def test_archive_action_button_class_maps_archive_and_restore() -> None:
    assert archive_action_button_class(archived_at=None) == (
        "admin-action admin-action--destructive"
    )
    assert archive_action_button_class(archived_at="2026-01-01") == (
        "admin-action admin-action--secondary"
    )


@pytest.mark.unit
def test_admin_action_css_resets_native_button_appearance() -> None:
    css = ADMIN_CSS.read_text(encoding="utf-8")
    base = _rule_block(css, ".admin-action {")
    assert "font-family: inherit" in base
    assert "padding:" in base
    assert "border:" in base
    assert "background:" in base
    assert "cursor: pointer" in base
    assert "border-radius:" in base

    secondary = _rule_block(css, ".admin-action--secondary {")
    assert "background:" in secondary
    assert "border-color:" in secondary
    assert "color:" in secondary
    assert "#fff" not in secondary.lower()
    assert "buttonface" not in secondary.lower()

    destructive = _rule_block(css, ".admin-action--destructive {")
    assert "background:" in destructive
    assert "border-color:" in destructive
    assert "color:" in destructive
    assert "#fff" not in destructive.lower()
    assert "buttonface" not in destructive.lower()


@pytest.mark.unit
def test_admin_action_css_includes_interaction_and_disabled_states() -> None:
    css = ADMIN_CSS.read_text(encoding="utf-8")
    assert ".admin-action--secondary:hover" in css
    assert ".admin-action--secondary:focus-visible" in css
    assert ".admin-action--secondary:active" in css
    assert ".admin-action--secondary:disabled" in css
    assert ".admin-action--destructive:hover" in css
    assert ".admin-action--destructive:focus-visible" in css
    assert ".admin-action--destructive:active" in css
    assert ".admin-action--destructive:disabled" in css

    secondary_disabled = _rule_block(css, ".admin-action--secondary:disabled {")
    assert "cursor: not-allowed" in secondary_disabled
    assert "opacity:" in secondary_disabled

    destructive_disabled = _rule_block(css, ".admin-action--destructive:disabled {")
    assert "cursor: not-allowed" in destructive_disabled
    assert "opacity:" in destructive_disabled

    secondary_focus = _rule_block(css, ".admin-action--secondary:focus-visible {")
    assert "outline:" in secondary_focus

    destructive_focus = _rule_block(css, ".admin-action--destructive:focus-visible {")
    assert "outline:" in destructive_focus
