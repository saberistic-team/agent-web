"""Regression tests for archive/restore admin action button styling (#233)."""

from __future__ import annotations

import re
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app import admin_contacts, admin_research_pages
from app.admin_auth import SESSION_COOKIE_NAME
from app.admin_layout import archive_restore_button_class
from app.admin_preview import (
    PREVIEW_CRM_DETAIL_COMPANY_ACTIVE_ID,
    PREVIEW_CRM_DETAIL_COMPANY_ARCHIVED_ID,
    PREVIEW_CRM_DETAIL_CONTACT_ACTIVE_ID,
    PREVIEW_CRM_DETAIL_CONTACT_ARCHIVED_ID,
    build_preview_company_research_detail,
    build_preview_contact_edit_detail,
    build_preview_contact_research_detail,
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


def _archive_button_markup(html: str) -> str:
    match = re.search(
        r'<button class="([^"]*)" type="submit">Archive (?:company|contact)</button>',
        html,
    )
    assert match is not None, html
    return match.group(1)


def _restore_button_markup(html: str) -> str:
    match = re.search(
        r'<button class="([^"]*)" type="submit">Restore (?:company|contact)</button>',
        html,
    )
    assert match is not None, html
    return match.group(1)


@pytest.mark.unit
def test_archive_restore_button_class_maps_semantic_variants() -> None:
    assert archive_restore_button_class(archived=False) == (
        "admin-action-btn admin-action-btn--destructive"
    )
    assert archive_restore_button_class(archived=True) == (
        "admin-action-btn admin-action-btn--restore"
    )


@pytest.mark.unit
def test_company_research_page_uses_themed_archive_and_restore_buttons() -> None:
    active_html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme"},
        contacts=[],
        records=[],
        csrf_token="csrf",
    )
    assert _archive_button_markup(active_html) == (
        "admin-action-btn admin-action-btn--destructive"
    )
    archive_form = active_html.split('action="/admin/companies/', 1)[1].split("</form>", 1)[0]
    assert 'class="admin-exit"' not in archive_form

    archived_html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme", "archived_at": "2026-07-01"},
        contacts=[],
        records=[],
        csrf_token="csrf",
    )
    assert _restore_button_markup(archived_html) == (
        "admin-action-btn admin-action-btn--restore"
    )


@pytest.mark.unit
def test_contact_research_and_edit_pages_use_themed_archive_and_restore_buttons() -> None:
    company = {"id": COMPANY_ID, "name": "Acme"}
    active_contact = {
        "id": CONTACT_ID,
        "full_name": "Jordan Preview",
        "company_id": COMPANY_ID,
        "buying_roles": [],
    }
    archived_contact = {**active_contact, "archived_at": "2026-07-01"}

    detail_active = admin_research_pages.render_admin_contact_research_page(
        contact=active_contact,
        company=company,
        records=[],
        csrf_token="csrf",
    )
    assert _archive_button_markup(detail_active) == (
        "admin-action-btn admin-action-btn--destructive"
    )

    detail_archived = admin_research_pages.render_admin_contact_research_page(
        contact=archived_contact,
        company=company,
        records=[],
        csrf_token="csrf",
    )
    assert _restore_button_markup(detail_archived) == (
        "admin-action-btn admin-action-btn--restore"
    )

    edit_active = admin_contacts.render_contact_form_page(
        admin_username="operator",
        csrf_token="csrf",
        companies=[company],
        contact=active_contact,
    )
    assert _archive_button_markup(edit_active) == (
        "admin-action-btn admin-action-btn--destructive"
    )

    edit_archived = admin_contacts.render_contact_form_page(
        admin_username="operator",
        csrf_token="csrf",
        companies=[company],
        contact=archived_contact,
    )
    assert _restore_button_markup(edit_archived) == (
        "admin-action-btn admin-action-btn--restore"
    )


@pytest.mark.unit
def test_admin_action_button_css_resets_native_button_appearance() -> None:
    css = _admin_css()
    base = _rule_block(css, ".admin-action-btn {")
    assert "appearance: none" in base
    assert "-webkit-appearance: none" in base
    assert "background:" in base
    assert "border:" in base
    assert "padding:" in base
    assert "font-family: inherit" in base
    assert "cursor: pointer" in base
    assert "border-radius:" in base

    destructive = _rule_block(css, ".admin-action-btn--destructive {")
    restore = _rule_block(css, ".admin-action-btn--restore {")
    assert "background:" in destructive
    assert "border-color:" in destructive
    assert "color:" in destructive
    assert "background:" in restore
    assert "border-color:" in restore

    assert ".admin-action-btn:focus-visible" in css
    assert ".admin-action-btn:active:not(:disabled)" in css
    assert ".admin-action-btn:disabled" in css
    assert ".admin-action-btn--destructive:hover:not(:disabled)" in css
    assert ".admin-action-btn--restore:hover:not(:disabled)" in css

    assert "buttonface" not in css
    assert "#ffffff" not in destructive.lower()
    assert "#fff" not in destructive.lower()


@pytest.mark.unit
def test_preview_crm_detail_builders_are_stable_and_populated() -> None:
    company_active = build_preview_company_research_detail(PREVIEW_CRM_DETAIL_COMPANY_ACTIVE_ID)
    company_archived = build_preview_company_research_detail(
        PREVIEW_CRM_DETAIL_COMPANY_ARCHIVED_ID
    )
    assert company_active is not None
    assert company_archived is not None
    active_company, active_contacts, active_records = company_active
    archived_company, _, _ = company_archived
    assert active_company["archived_at"] is None
    assert archived_company["archived_at"] is not None
    assert active_contacts
    assert active_records

    contact_active = build_preview_contact_research_detail(PREVIEW_CRM_DETAIL_CONTACT_ACTIVE_ID)
    contact_archived = build_preview_contact_research_detail(
        PREVIEW_CRM_DETAIL_CONTACT_ARCHIVED_ID
    )
    assert contact_active is not None
    assert contact_archived is not None
    active_row, _, _ = contact_active
    archived_row, _, _ = contact_archived
    assert active_row["archived_at"] is None
    assert archived_row["archived_at"] is not None

    edit_active = build_preview_contact_edit_detail(PREVIEW_CRM_DETAIL_CONTACT_ACTIVE_ID)
    edit_archived = build_preview_contact_edit_detail(PREVIEW_CRM_DETAIL_CONTACT_ARCHIVED_ID)
    assert edit_active is not None
    assert edit_archived is not None


@pytest.mark.unit
@pytest.mark.integration
def test_preview_routes_render_archive_and_restore_action_buttons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "42")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cookie = {SESSION_COOKIE_NAME: "preview-screenshot-session"}

    company_archive = client.get(
        f"/admin/companies/{PREVIEW_CRM_DETAIL_COMPANY_ACTIVE_ID}",
        cookies=cookie,
    )
    assert company_archive.status_code == 200
    assert "admin-action-btn--destructive" in company_archive.text
    assert "Archive company" in company_archive.text

    company_restore = client.get(
        f"/admin/companies/{PREVIEW_CRM_DETAIL_COMPANY_ARCHIVED_ID}",
        cookies=cookie,
    )
    assert company_restore.status_code == 200
    assert "admin-action-btn--restore" in company_restore.text
    assert "Restore company" in company_restore.text

    contact_archive = client.get(
        f"/admin/contacts/{PREVIEW_CRM_DETAIL_CONTACT_ACTIVE_ID}",
        cookies=cookie,
    )
    assert contact_archive.status_code == 200
    assert "admin-action-btn--destructive" in contact_archive.text

    contact_restore = client.get(
        f"/admin/contacts/{PREVIEW_CRM_DETAIL_CONTACT_ARCHIVED_ID}",
        cookies=cookie,
    )
    assert contact_restore.status_code == 200
    assert "admin-action-btn--restore" in contact_restore.text

    edit_archive = client.get(
        f"/admin/contacts/{PREVIEW_CRM_DETAIL_CONTACT_ACTIVE_ID}/edit",
        cookies=cookie,
    )
    assert edit_archive.status_code == 200
    assert "admin-action-btn--destructive" in edit_archive.text

    edit_restore = client.get(
        f"/admin/contacts/{PREVIEW_CRM_DETAIL_CONTACT_ARCHIVED_ID}/edit",
        cookies=cookie,
    )
    assert edit_restore.status_code == 200
    assert "admin-action-btn--restore" in edit_restore.text
