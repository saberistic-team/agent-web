"""Tests for themed archive/restore admin action buttons (#233)."""

from __future__ import annotations

import random
import re
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app import admin_contacts, admin_research_pages
from app.admin_auth import SESSION_COOKIE_NAME
from app.admin_layout import render_archive_restore_button
from app.admin_preview import (
    PREVIEW_COMPANY_ARCHIVE_ID,
    PREVIEW_COMPANY_RESTORE_ID,
    PREVIEW_CONTACT_ARCHIVE_ID,
    PREVIEW_CONTACT_RESTORE_ID,
    build_preview_company_detail,
    build_preview_contact_detail,
    build_preview_contact_form,
)
from app.main import app

client = TestClient(app, follow_redirects=False)

ADMIN_CSS = Path(__file__).resolve().parents[1] / "site/assets/admin.css"
COMPANY_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
CONTACT_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


def _rule_block(css: str, selector_fragment: str) -> str:
    start = css.index(selector_fragment)
    brace = css.index("{", start)
    depth = 0
    for index, char in enumerate(css[brace:], start=brace):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return css[brace : index + 1]
    raise AssertionError(f"Unclosed rule for {selector_fragment!r}")


@pytest.mark.unit
def test_render_archive_restore_button_uses_semantic_classes() -> None:
    archive = render_archive_restore_button(label="Archive company", is_restore=False)
    restore = render_archive_restore_button(label="Restore company", is_restore=True)
    assert 'class="admin-action-button admin-action-destructive"' in archive
    assert "Archive company" in archive
    assert 'class="admin-action-button admin-action-restore"' in restore
    assert "Restore company" in restore
    assert "admin-exit" not in archive
    assert "admin-exit" not in restore


@pytest.mark.unit
def test_company_research_page_archive_button_markup() -> None:
    html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme"},
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert 'action="/admin/companies/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/archive"' in html
    assert 'class="admin-action-button admin-action-destructive"' in html
    assert "Archive company" in html
    assert 'admin-exit" type="submit">Archive company' not in html


@pytest.mark.unit
def test_company_research_page_restore_button_markup() -> None:
    html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme", "archived_at": "2026-01-01"},
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert 'action="/admin/companies/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/restore"' in html
    assert 'class="admin-action-button admin-action-restore"' in html
    assert "Restore company" in html


@pytest.mark.unit
def test_contact_research_and_edit_pages_use_themed_buttons() -> None:
    detail_archive = admin_research_pages.render_admin_contact_research_page(
        contact={"id": CONTACT_ID, "full_name": "Pat", "buying_roles": []},
        company=None,
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert 'class="admin-action-button admin-action-destructive"' in detail_archive
    assert "Archive contact" in detail_archive

    detail_restore = admin_research_pages.render_admin_contact_research_page(
        contact={"id": CONTACT_ID, "full_name": "Pat", "archived_at": "2026-01-01", "buying_roles": []},
        company=None,
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert 'class="admin-action-button admin-action-restore"' in detail_restore
    assert "Restore contact" in detail_restore

    edit_archive = admin_contacts.render_contact_form_page(
        admin_username="operator",
        csrf_token="csrf",
        companies=[],
        contact={"id": CONTACT_ID, "full_name": "Pat"},
    )
    assert 'class="admin-action-button admin-action-destructive"' in edit_archive
    assert "Archive contact" in edit_archive

    edit_restore = admin_contacts.render_contact_form_page(
        admin_username="operator",
        csrf_token="csrf",
        companies=[],
        contact={"id": CONTACT_ID, "full_name": "Pat", "archived_at": "2026-01-01"},
    )
    assert 'class="admin-action-button admin-action-restore"' in edit_restore
    assert "Restore contact" in edit_restore


@pytest.mark.unit
def test_admin_action_button_css_resets_native_appearance() -> None:
    css = ADMIN_CSS.read_text(encoding="utf-8")
    base_block = _rule_block(css, ".admin-action-button {")
    destructive_block = _rule_block(css, ".admin-action-destructive {")
    restore_block = _rule_block(css, ".admin-action-restore {")

    assert "background:" in base_block
    assert "border" in base_block
    assert "cursor:" in base_block
    assert "background:" in destructive_block
    assert "border" in destructive_block
    assert "background:" in restore_block
    assert "border" in restore_block

    assert "font-family: inherit" in base_block
    assert "padding:" in base_block
    assert "border-radius:" in base_block

    assert ":focus-visible" in css
    assert ".admin-action-destructive:hover" in css
    assert ".admin-action-destructive:disabled" in css
    assert ".admin-action-restore:hover" in css
    assert ".admin-action-restore:disabled" in css
    assert ".admin-action-destructive:active" in css
    assert ".admin-action-restore:active" in css

    destructive_focus = _rule_block(css, ".admin-action-destructive:focus-visible")
    restore_focus = _rule_block(css, ".admin-action-restore:focus-visible")
    assert "outline:" in destructive_focus
    assert "outline:" in restore_focus

    destructive_disabled = _rule_block(css, ".admin-action-destructive:disabled")
    restore_disabled = _rule_block(css, ".admin-action-restore:disabled")
    assert "cursor: not-allowed" in destructive_disabled
    assert "cursor: not-allowed" in restore_disabled
    assert "opacity:" in destructive_disabled
    assert "opacity:" in restore_disabled

    # Destructive and restore use themed surfaces, not browser-default white buttons.
    assert "#fff" not in destructive_block.lower()
    assert "white" not in destructive_block.lower()
    assert "buttonface" not in destructive_block.lower()
    assert "var(--surface)" in destructive_block or "color-mix" in destructive_block


@pytest.mark.unit
def test_preview_company_detail_states_are_stable_with_seed() -> None:
    archive = build_preview_company_detail(PREVIEW_COMPANY_ARCHIVE_ID, rng=random.Random(9))
    restore = build_preview_company_detail(PREVIEW_COMPANY_RESTORE_ID, rng=random.Random(9))
    assert archive is not None
    assert restore is not None
    archive_company, archive_contacts, archive_records = archive
    restore_company, restore_contacts, restore_records = restore
    assert archive_company["archived_at"] is None
    assert restore_company["archived_at"] is not None
    assert archive_contacts
    assert archive_records
    assert archive_company["name"] == build_preview_company_detail(
        PREVIEW_COMPANY_ARCHIVE_ID, rng=random.Random(9)
    )[0]["name"]


@pytest.mark.unit
def test_preview_contact_detail_and_form_states() -> None:
    archive_detail = build_preview_contact_detail(PREVIEW_CONTACT_ARCHIVE_ID, rng=random.Random(3))
    restore_detail = build_preview_contact_detail(PREVIEW_CONTACT_RESTORE_ID, rng=random.Random(3))
    assert archive_detail is not None
    assert restore_detail is not None
    archive_contact, _, _ = archive_detail
    restore_contact, _, _ = restore_detail
    assert archive_contact["archived_at"] is None
    assert restore_contact["archived_at"] is not None

    archive_form = build_preview_contact_form(PREVIEW_CONTACT_ARCHIVE_ID, rng=random.Random(3))
    restore_form = build_preview_contact_form(PREVIEW_CONTACT_RESTORE_ID, rng=random.Random(3))
    assert archive_form is not None
    assert restore_form is not None
    assert archive_form["archived_at"] is None
    assert restore_form["archived_at"] is not None


@pytest.mark.unit
@pytest.mark.integration
def test_preview_routes_render_archive_and_restore_buttons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "42")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    archive_company = client.get(
        f"/admin/companies/{PREVIEW_COMPANY_ARCHIVE_ID}",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert archive_company.status_code == 200
    assert 'class="admin-action-button admin-action-destructive"' in archive_company.text
    assert "Archive company" in archive_company.text

    restore_company = client.get(
        f"/admin/companies/{PREVIEW_COMPANY_RESTORE_ID}",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert restore_company.status_code == 200
    assert 'class="admin-action-button admin-action-restore"' in restore_company.text
    assert "Restore company" in restore_company.text

    archive_contact = client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_ARCHIVE_ID}",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert archive_contact.status_code == 200
    assert 'class="admin-action-button admin-action-destructive"' in archive_contact.text
    assert "Archive contact" in archive_contact.text

    restore_contact = client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_RESTORE_ID}",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert restore_contact.status_code == 200
    assert 'class="admin-action-button admin-action-restore"' in restore_contact.text
    assert "Restore contact" in restore_contact.text

    restore_edit = client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_RESTORE_ID}/edit",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert restore_edit.status_code == 200
    assert 'class="admin-action-button admin-action-restore"' in restore_edit.text
    assert "Restore contact" in restore_edit.text
    assert re.search(r'class="cta admin-submit".*Save contact', restore_edit.text, re.DOTALL)
