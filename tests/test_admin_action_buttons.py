"""Regression tests for admin archive/restore action button styling (#233)."""

from __future__ import annotations

import re
from pathlib import Path
from uuid import UUID

import pytest

from app import admin_contacts, admin_research_pages
from app.admin_layout import archive_action_button_class

ADMIN_CSS = Path(__file__).resolve().parents[1] / "site/assets/admin.css"

COMPANY_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
CONTACT_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


def _admin_css() -> str:
    return ADMIN_CSS.read_text(encoding="utf-8")


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


def _archive_button_markup(html: str) -> str | None:
    match = re.search(
        r'<button class="admin-action[^"]*"[^>]*>(?:Archive|Restore) (?:company|contact)</button>',
        html,
    )
    return match.group(0) if match else None


@pytest.mark.unit
def test_archive_action_button_class_variants() -> None:
    assert archive_action_button_class(archived=False) == "admin-action admin-action--archive"
    assert archive_action_button_class(archived=True) == "admin-action admin-action--restore"


@pytest.mark.unit
def test_company_research_archive_button_uses_destructive_action_class() -> None:
    html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme"},
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    button = _archive_button_markup(html)
    assert button is not None
    assert 'class="admin-action admin-action--archive"' in button
    assert "Archive company" in button
    assert "admin-exit" not in button


@pytest.mark.unit
def test_company_research_restore_button_uses_restore_action_class() -> None:
    html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme", "archived_at": "2026-01-01"},
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    button = _archive_button_markup(html)
    assert button is not None
    assert 'class="admin-action admin-action--restore"' in button
    assert "Restore company" in button
    assert "admin-exit" not in button


@pytest.mark.unit
def test_contact_research_archive_and_restore_buttons() -> None:
    contact = {
        "id": CONTACT_ID,
        "full_name": "Ada",
        "company_id": COMPANY_ID,
        "buying_roles": [],
    }
    company = {"id": COMPANY_ID, "name": "Acme"}

    archive_html = admin_research_pages.render_admin_contact_research_page(
        contact=contact,
        company=company,
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    archive_button = _archive_button_markup(archive_html)
    assert archive_button is not None
    assert 'class="admin-action admin-action--archive"' in archive_button
    assert "Archive contact" in archive_button

    restore_html = admin_research_pages.render_admin_contact_research_page(
        contact={**contact, "archived_at": "2026-01-01"},
        company=company,
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    restore_button = _archive_button_markup(restore_html)
    assert restore_button is not None
    assert 'class="admin-action admin-action--restore"' in restore_button
    assert "Restore contact" in restore_button


@pytest.mark.unit
def test_contact_edit_archive_and_restore_buttons() -> None:
    archive_html = admin_contacts.render_contact_form_page(
        admin_username="operator",
        csrf_token="csrf",
        companies=[],
        contact={"id": CONTACT_ID, "full_name": "Ada"},
    )
    archive_button = _archive_button_markup(archive_html)
    assert archive_button is not None
    assert 'class="admin-action admin-action--archive"' in archive_button
    assert "Archive contact" in archive_button

    restore_html = admin_contacts.render_contact_form_page(
        admin_username="operator",
        csrf_token="csrf",
        companies=[],
        contact={"id": CONTACT_ID, "full_name": "Ada", "archived_at": "2026-01-01"},
    )
    restore_button = _archive_button_markup(restore_html)
    assert restore_button is not None
    assert 'class="admin-action admin-action--restore"' in restore_button
    assert "Restore contact" in restore_button


@pytest.mark.unit
def test_admin_action_css_resets_native_button_appearance() -> None:
    css = _admin_css()
    base = _rule_block(css, ".admin-action {")
    assert "appearance: none" in base
    assert "-webkit-appearance: none" in base
    assert "background:" in base
    assert "border:" in base
    assert "padding:" in base
    assert "font-family:" in base
    assert "color:" in base
    assert "cursor: pointer" in base
    assert "border-radius:" in base
    assert "background: transparent" in base
    assert re.search(r"background:\s*#fff", base, re.IGNORECASE) is None
    assert re.search(r"background:\s*white", base, re.IGNORECASE) is None


@pytest.mark.unit
def test_admin_action_css_archive_and_restore_interaction_states() -> None:
    css = _admin_css()
    archive = _rule_block(css, ".admin-action--archive {")
    restore = _rule_block(css, ".admin-action--restore {")
    assert ":hover" in css.split(".admin-action--archive {", 1)[1]
    assert ":focus-visible" in css.split(".admin-action--archive {", 1)[1]
    assert ":active" in css.split(".admin-action--archive {", 1)[1]
    assert ":disabled" in css.split(".admin-action--archive {", 1)[1]
    assert "outline" in css.split(".admin-action--archive:focus-visible", 1)[1]
    assert "color:" in archive
    assert "border-color:" in archive
    assert "background:" in archive
    assert ":hover" in css.split(".admin-action--restore {", 1)[1]
    assert ":focus-visible" in css.split(".admin-action--restore {", 1)[1]
    assert ":active" in css.split(".admin-action--restore {", 1)[1]
    assert ":disabled" in css.split(".admin-action--restore {", 1)[1]
    assert "outline" in css.split(".admin-action--restore:focus-visible", 1)[1]
    assert "color:" in restore
    assert "border-color:" in restore
    assert "background:" in restore
    restore_section = css.split(".admin-action--restore {", 1)[1].split(".admin-layout", 1)[0]
    assert "var(--accent)" in restore_section


@pytest.mark.unit
def test_admin_action_css_distinguishes_archive_from_primary_cta() -> None:
    css = _admin_css()
    archive = _rule_block(css, ".admin-action--archive {")
    assert "var(--accent)" not in archive
