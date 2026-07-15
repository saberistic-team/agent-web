"""Regression tests for themed Archive/Restore admin action buttons (#233)."""

from __future__ import annotations

import re
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app import admin_contacts, admin_research_pages
from app.admin_auth import SESSION_COOKIE_NAME
from app.admin_preview import (
    PREVIEW_CRM_COMPANY_ACTIVE_ID,
    PREVIEW_CRM_COMPANY_ARCHIVED_ID,
    PREVIEW_CRM_CONTACT_ACTIVE_ID,
    PREVIEW_CRM_CONTACT_ARCHIVED_ID,
    build_preview_company_detail,
    build_preview_contact_detail,
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


def _archive_button_markup(html: str, label: str) -> str | None:
    pattern = (
        rf'<button class="([^"]+)" type="submit">{re.escape(label)}</button>'
    )
    match = re.search(pattern, html)
    return match.group(1) if match else None


@pytest.mark.unit
def test_admin_action_resets_native_button_appearance() -> None:
    css = _admin_css()
    block = _rule_block(css, ".admin-action {")
    assert "background:" in block
    assert "border:" in block
    assert "padding:" in block
    assert "font-family: inherit" in block
    assert "color:" in block
    assert "cursor: pointer" in block
    assert "border-radius:" in block


@pytest.mark.unit
def test_admin_action_destructive_and_restore_modifiers_have_states() -> None:
    css = _admin_css()
    destructive = _rule_block(css, ".admin-action--destructive {")
    restore = _rule_block(css, ".admin-action--restore {")
    assert "background:" in destructive
    assert "border-color:" in destructive
    assert "color:" in destructive
    assert ".admin-action--destructive:hover" in css
    assert ".admin-action--destructive:focus-visible" in css
    assert ".admin-action--destructive:active" in css
    assert ".admin-action--destructive:disabled" in css
    assert "background:" in restore
    assert ".admin-action--restore:hover" in css
    assert ".admin-action--restore:focus-visible" in css
    assert ".admin-action--restore:disabled" in css
    disabled = _rule_block(css, ".admin-action:disabled,")
    assert "cursor: not-allowed" in disabled
    assert "opacity:" in disabled


@pytest.mark.unit
def test_admin_action_focus_visible_uses_outline() -> None:
    css = _admin_css()
    block = _rule_block(css, ".admin-action:focus-visible {")
    assert "outline: 2px solid var(--accent)" in block
    assert "outline-offset: 2px" in block


@pytest.mark.unit
def test_admin_css_asset_served_includes_action_classes() -> None:
    response = client.get("/assets/admin.css")
    assert response.status_code == 200
    body = response.text
    assert ".admin-action {" in body
    assert ".admin-action--destructive {" in body
    assert ".admin-action--restore {" in body


@pytest.mark.unit
def test_company_research_page_archive_uses_destructive_action_class() -> None:
    html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme"},
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    classes = _archive_button_markup(html, "Archive company")
    assert classes == "admin-action admin-action--destructive"
    assert 'class="admin-exit" type="submit">Archive company' not in html


@pytest.mark.unit
def test_company_research_page_restore_uses_restore_action_class() -> None:
    html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme", "archived_at": "2026-01-01"},
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    classes = _archive_button_markup(html, "Restore company")
    assert classes == "admin-action admin-action--restore"


@pytest.mark.unit
def test_contact_research_page_archive_uses_destructive_action_class() -> None:
    html = admin_research_pages.render_admin_contact_research_page(
        contact={
            "id": CONTACT_ID,
            "full_name": "Ada",
            "company_id": COMPANY_ID,
            "buying_roles": [],
        },
        company={"id": COMPANY_ID, "name": "Acme"},
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    classes = _archive_button_markup(html, "Archive contact")
    assert classes == "admin-action admin-action--destructive"


@pytest.mark.unit
def test_contact_research_page_restore_uses_restore_action_class() -> None:
    html = admin_research_pages.render_admin_contact_research_page(
        contact={
            "id": CONTACT_ID,
            "full_name": "Ada",
            "archived_at": "2026-01-01",
            "company_id": COMPANY_ID,
            "buying_roles": [],
        },
        company={"id": COMPANY_ID, "name": "Acme"},
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    classes = _archive_button_markup(html, "Restore contact")
    assert classes == "admin-action admin-action--restore"


@pytest.mark.unit
def test_contact_edit_page_archive_and_restore_action_classes() -> None:
    companies = [{"id": COMPANY_ID, "name": "Acme"}]
    archive_html = admin_contacts.render_contact_form_page(
        csrf_token="csrf",
        admin_username="operator",
        companies=companies,
        contact={"id": CONTACT_ID, "full_name": "Pat"},
    )
    assert (
        _archive_button_markup(archive_html, "Archive contact")
        == "admin-action admin-action--destructive"
    )

    restore_html = admin_contacts.render_contact_form_page(
        csrf_token="csrf",
        admin_username="operator",
        companies=companies,
        contact={"id": CONTACT_ID, "full_name": "Pat", "archived_at": "2026-01-01"},
    )
    assert (
        _archive_button_markup(restore_html, "Restore contact")
        == "admin-action admin-action--restore"
    )


@pytest.mark.unit
def test_preview_company_detail_pages_render_archive_and_restore_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "42")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    active = client.get(
        f"/admin/companies/{PREVIEW_CRM_COMPANY_ACTIVE_ID}",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert active.status_code == 200
    assert (
        _archive_button_markup(active.text, "Archive company")
        == "admin-action admin-action--destructive"
    )
    preview = build_preview_company_detail(PREVIEW_CRM_COMPANY_ACTIVE_ID)
    assert preview is not None
    assert preview[0]["name"] in active.text

    archived = client.get(
        f"/admin/companies/{PREVIEW_CRM_COMPANY_ARCHIVED_ID}",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert archived.status_code == 200
    assert (
        _archive_button_markup(archived.text, "Restore company")
        == "admin-action admin-action--restore"
    )


@pytest.mark.unit
def test_preview_contact_detail_and_edit_render_archive_and_restore_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "42")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    active_detail = client.get(
        f"/admin/contacts/{PREVIEW_CRM_CONTACT_ACTIVE_ID}",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert active_detail.status_code == 200
    assert (
        _archive_button_markup(active_detail.text, "Archive contact")
        == "admin-action admin-action--destructive"
    )
    preview = build_preview_contact_detail(PREVIEW_CRM_CONTACT_ACTIVE_ID)
    assert preview is not None
    assert preview[0]["full_name"] in active_detail.text

    archived_detail = client.get(
        f"/admin/contacts/{PREVIEW_CRM_CONTACT_ARCHIVED_ID}",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert archived_detail.status_code == 200
    assert (
        _archive_button_markup(archived_detail.text, "Restore contact")
        == "admin-action admin-action--restore"
    )

    archived_edit = client.get(
        f"/admin/contacts/{PREVIEW_CRM_CONTACT_ARCHIVED_ID}/edit",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert archived_edit.status_code == 200
    assert (
        _archive_button_markup(archived_edit.text, "Restore contact")
        == "admin-action admin-action--restore"
    )
