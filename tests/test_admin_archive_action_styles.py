"""Regression tests for themed Archive/Restore admin action buttons (#233)."""

from __future__ import annotations

import re
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app import admin_contacts, admin_research_pages
from app.admin_auth import SESSION_COOKIE_NAME
from app.admin_layout import render_archive_action_button
from app.admin_preview import (
    PREVIEW_COMPANY_ARCHIVE_DETAIL_ID,
    PREVIEW_COMPANY_RESTORE_DETAIL_ID,
    PREVIEW_CONTACT_ARCHIVE_DETAIL_ID,
    PREVIEW_CONTACT_RESTORE_DETAIL_ID,
)
from app.main import app

ADMIN_CSS = Path(__file__).resolve().parents[1] / "site/assets/admin.css"

COMPANY_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
CONTACT_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

client = TestClient(app, follow_redirects=False)


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
        r'<button class="admin-action-btn[^"]*" type="submit">[^<]+</button>',
        html,
    )
    return match.group(0) if match else None


@pytest.mark.unit
def test_render_archive_action_button_uses_semantic_classes() -> None:
    archive = render_archive_action_button(label="Archive company", is_restore=False)
    restore = render_archive_action_button(label="Restore company", is_restore=True)
    assert 'class="admin-action-btn admin-action-btn--destructive"' in archive
    assert "Archive company" in archive
    assert 'class="admin-action-btn admin-action-btn--restore"' in restore
    assert "Restore company" in restore
    assert "admin-exit" not in archive
    assert "admin-exit" not in restore


@pytest.mark.unit
def test_admin_action_btn_resets_native_button_appearance() -> None:
    css = _admin_css()
    block = _rule_block(css, ".admin-action-btn {")
    assert "appearance: none" in block
    assert "-webkit-appearance: none" in block
    assert "background:" in block
    assert "border:" in block
    assert "padding:" in block
    assert "font-family: inherit" in block
    assert "color:" in block
    assert "cursor: pointer" in block
    assert "border-radius:" in block


@pytest.mark.unit
def test_admin_action_btn_states_cover_interaction_and_disabled() -> None:
    css = _admin_css()
    assert ".admin-action-btn:hover" in css
    assert ".admin-action-btn:focus-visible" in css
    assert ".admin-action-btn:active:not(:disabled)" in css
    assert ".admin-action-btn:disabled" in css
    assert ".admin-action-btn--destructive:hover:not(:disabled)" in css
    assert ".admin-action-btn--destructive:focus-visible" in css
    assert ".admin-action-btn--destructive:disabled" in css
    assert ".admin-action-btn--restore:hover:not(:disabled)" in css
    assert ".admin-action-btn--restore:focus-visible" in css


@pytest.mark.unit
def test_admin_action_btn_variants_differ_from_primary_cta() -> None:
    css = _admin_css()
    destructive = _rule_block(css, ".admin-action-btn--destructive {")
    restore = _rule_block(css, ".admin-action-btn--restore {")
    assert "var(--accent)" not in destructive.split("background:")[1].split(";")[0]
    assert "background: var(--accent)" not in destructive
    assert "background: var(--accent)" not in restore


@pytest.mark.unit
def test_company_research_page_renders_themed_archive_and_restore_buttons() -> None:
    archive_html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme"},
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    archive_button = _archive_button_markup(archive_html)
    assert archive_button is not None
    assert 'admin-action-btn--destructive' in archive_button
    assert "Archive company" in archive_button
    assert 'class="admin-exit" type="submit">Archive company' not in archive_html

    restore_html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme", "archived_at": "2026-01-01"},
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    restore_button = _archive_button_markup(restore_html)
    assert restore_button is not None
    assert 'admin-action-btn--restore' in restore_button
    assert "Restore company" in restore_button


@pytest.mark.unit
def test_contact_research_and_edit_pages_render_themed_archive_buttons() -> None:
    contact = {
        "id": CONTACT_ID,
        "full_name": "Pat",
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
    detail_button = _archive_button_markup(detail_html)
    assert detail_button is not None
    assert 'admin-action-btn--destructive' in detail_button
    assert "Archive contact" in detail_button

    edit_html = admin_contacts.render_contact_form_page(
        csrf_token="csrf",
        admin_username="operator",
        companies=[{"id": COMPANY_ID, "name": "Acme"}],
        contact={**contact, "archived_at": "2026-01-01"},
    )
    edit_button = _archive_button_markup(edit_html)
    assert edit_button is not None
    assert 'admin-action-btn--restore' in edit_button
    assert "Restore contact" in edit_button
    assert 'class="admin-exit" type="submit">Restore contact' not in edit_html
    assert 'class="admin-exit" type="submit">Archive contact' not in detail_html


@pytest.mark.unit
@pytest.mark.integration
def test_admin_css_asset_served_with_action_button_rules() -> None:
    response = client.get("/assets/admin.css")
    assert response.status_code == 200
    body = response.text
    assert ".admin-action-btn {" in body
    assert ".admin-action-btn--destructive {" in body
    assert ".admin-action-btn--restore {" in body
    assert "appearance: none" in body


@pytest.mark.unit
@pytest.mark.integration
def test_preview_company_and_contact_detail_pages_include_archive_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "233")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    company_archive = client.get(
        f"/admin/companies/{PREVIEW_COMPANY_ARCHIVE_DETAIL_ID}",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert company_archive.status_code == 200
    assert "Northwind Labs" in company_archive.text
    assert 'admin-action-btn--destructive' in company_archive.text
    assert "Archive company" in company_archive.text

    company_restore = client.get(
        f"/admin/companies/{PREVIEW_COMPANY_RESTORE_DETAIL_ID}",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert company_restore.status_code == 200
    assert "Helios Rail" in company_restore.text
    assert 'admin-action-btn--restore' in company_restore.text
    assert "Restore company" in company_restore.text

    contact_archive = client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_ARCHIVE_DETAIL_ID}",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert contact_archive.status_code == 200
    assert "Alex Nguyen" in contact_archive.text
    assert 'admin-action-btn--destructive' in contact_archive.text

    contact_restore = client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_RESTORE_DETAIL_ID}/edit",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert contact_restore.status_code == 200
    assert "Sam Patel" in contact_restore.text
    assert 'admin-action-btn--restore' in contact_restore.text
