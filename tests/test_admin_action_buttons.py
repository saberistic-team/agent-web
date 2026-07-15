"""Regression tests for themed admin Archive/Restore action buttons (#233)."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest

from app import admin_contacts, admin_research_pages

ADMIN_CSS = Path(__file__).resolve().parents[1] / "site/assets/admin.css"
COMPANY_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
CONTACT_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


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


def _admin_css() -> str:
    return ADMIN_CSS.read_text(encoding="utf-8")


@pytest.mark.unit
def test_admin_action_css_resets_native_button_appearance() -> None:
    css = _admin_css()
    base = _rule_block(css, ".admin-action {")
    for token in (
        "background:",
        "border:",
        "padding:",
        "font-family:",
        "color:",
        "cursor:",
        "border-radius:",
    ):
        assert token in base

    destructive = _rule_block(css, ".admin-action--destructive {")
    secondary = _rule_block(css, ".admin-action--secondary {")
    assert "background:" in destructive
    assert "border-color:" in destructive
    assert "color:" in destructive
    assert "background:" in secondary
    assert "border-color:" in secondary
    assert "color:" in secondary


@pytest.mark.unit
def test_admin_action_css_includes_interaction_and_disabled_states() -> None:
    css = _admin_css()
    assert ".admin-action:hover:not(:disabled)" in css
    assert ".admin-action:focus-visible:not(:disabled)" in css
    assert ".admin-action:active:not(:disabled)" in css
    assert ".admin-action:disabled" in css
    assert "outline: 2px solid var(--accent)" in css
    assert "cursor: not-allowed" in css


@pytest.mark.unit
def test_company_research_archive_and_restore_use_semantic_action_classes() -> None:
    archive_html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme"},
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert 'class="admin-action admin-action--destructive" type="submit">Archive company' in archive_html
    archive_form = archive_html.split('action="/admin/companies/')[1].split("</form>")[0]
    assert "admin-exit" not in archive_form

    restore_html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme", "archived_at": "2026-01-01"},
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert 'class="admin-action admin-action--secondary" type="submit">Restore company' in restore_html


@pytest.mark.unit
def test_contact_research_archive_and_restore_use_semantic_action_classes() -> None:
    archive_html = admin_research_pages.render_admin_contact_research_page(
        contact={"id": CONTACT_ID, "full_name": "Ada"},
        company=None,
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert 'class="admin-action admin-action--destructive" type="submit">Archive contact' in archive_html

    restore_html = admin_research_pages.render_admin_contact_research_page(
        contact={"id": CONTACT_ID, "full_name": "Ada", "archived_at": "2026-01-01"},
        company=None,
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert 'class="admin-action admin-action--secondary" type="submit">Restore contact' in restore_html


@pytest.mark.unit
def test_contact_edit_archive_and_restore_use_semantic_action_classes() -> None:
    archive_html = admin_contacts.render_contact_form_page(
        admin_username="operator",
        csrf_token="csrf",
        companies=[],
        contact={"id": CONTACT_ID, "full_name": "Ada"},
    )
    assert 'class="admin-action admin-action--destructive" type="submit">Archive contact' in archive_html
    archive_form = archive_html.split("/archive\">")[1].split("</form>")[0]
    assert "admin-exit" not in archive_form

    restore_html = admin_contacts.render_contact_form_page(
        admin_username="operator",
        csrf_token="csrf",
        companies=[],
        contact={"id": CONTACT_ID, "full_name": "Ada", "archived_at": "2026-01-01"},
    )
    assert 'class="admin-action admin-action--secondary" type="submit">Restore contact' in restore_html
