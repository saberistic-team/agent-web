"""Tests for themed Archive/Restore admin action buttons (#233)."""

from __future__ import annotations

import re
from pathlib import Path
from uuid import UUID

import pytest

from app import admin_contacts, admin_research_pages
from app.admin_layout import render_admin_archive_restore_button

COMPANY_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
CONTACT_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
ADMIN_CSS = Path(__file__).resolve().parents[1] / "site/assets/admin.css"


@pytest.mark.unit
def test_render_admin_archive_restore_button_uses_semantic_modifiers() -> None:
    archive = render_admin_archive_restore_button(label="Archive company", archived=False)
    restore = render_admin_archive_restore_button(label="Restore company", archived=True)
    assert 'class="admin-action admin-action--destructive"' in archive
    assert 'class="admin-action admin-action--secondary"' in restore
    assert "admin-exit" not in archive
    assert "admin-exit" not in restore


@pytest.mark.unit
def test_company_research_page_renders_themed_archive_and_restore_buttons() -> None:
    company = {"id": COMPANY_ID, "name": "Acme", "status": "prospect"}
    archive_html = admin_research_pages.render_admin_company_research_page(
        company=company,
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert 'class="admin-action admin-action--destructive"' in archive_html
    assert "Archive company" in archive_html
    assert 'action="/admin/companies/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/archive"' in archive_html
    assert 'class="admin-exit" type="submit">Archive company' not in archive_html

    restore_html = admin_research_pages.render_admin_company_research_page(
        company={**company, "archived_at": "2026-01-01"},
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert 'class="admin-action admin-action--secondary"' in restore_html
    assert "Restore company" in restore_html
    assert 'action="/admin/companies/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/restore"' in restore_html


@pytest.mark.unit
def test_contact_research_and_edit_pages_render_themed_archive_buttons() -> None:
    contact = {
        "id": CONTACT_ID,
        "full_name": "Pat Example",
        "company_id": COMPANY_ID,
        "buying_roles": [],
    }
    detail_html = admin_research_pages.render_admin_contact_research_page(
        contact=contact,
        company={"id": COMPANY_ID, "name": "Acme"},
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert 'class="admin-action admin-action--destructive"' in detail_html
    assert "Archive contact" in detail_html
    assert 'class="admin-record-action"' in detail_html

    edit_html = admin_contacts.render_contact_form_page(
        admin_username="operator",
        csrf_token="csrf",
        companies=[{"id": COMPANY_ID, "name": "Acme"}],
        contact={**contact, "archived_at": "2026-01-01"},
    )
    assert 'class="admin-action admin-action--secondary"' in edit_html
    assert "Restore contact" in edit_html
    assert 'class="admin-exit" type="submit">Restore contact' not in edit_html


@pytest.mark.unit
def test_admin_action_css_resets_native_button_appearance() -> None:
    css = ADMIN_CSS.read_text(encoding="utf-8")
    action_block = css.split(".admin-action {", 1)[1].split("}", 1)[0]
    assert "font-family: inherit" in action_block
    assert "cursor: pointer" in action_block
    assert "border-radius: 2px" in action_block
    assert "padding:" in action_block
    assert "background:" in action_block
    assert "border:" in action_block

    destructive_block = css.split(".admin-action--destructive {", 1)[1].split("}", 1)[0]
    assert "#ffb4b4" in destructive_block
    assert "background:" in destructive_block
    assert re.search(r"background:\s*color-mix", destructive_block)
    assert "white" not in destructive_block.lower()

    secondary_block = css.split(".admin-action--secondary {", 1)[1].split("}", 1)[0]
    assert "var(--line)" in secondary_block
    assert "var(--ink)" in secondary_block

    assert ".admin-action--destructive:focus-visible" in css
    assert ".admin-action--secondary:focus-visible" in css
    assert ".admin-action--destructive:active" in css
    assert ".admin-action--secondary:active" in css
    assert ".admin-action:disabled" in css
    assert "cursor: not-allowed" in css.split(".admin-action:disabled", 1)[1]


@pytest.mark.unit
def test_admin_exit_styling_remains_separate_from_form_actions() -> None:
    css = ADMIN_CSS.read_text(encoding="utf-8")
    exit_block = css.split(".admin-exit {", 1)[1].split("}", 1)[0]
    assert "text-decoration: none" in exit_block
    assert "border-bottom:" in exit_block
    assert "padding:" not in exit_block
    assert ".admin-action" in css
    assert ".admin-exit" in css
