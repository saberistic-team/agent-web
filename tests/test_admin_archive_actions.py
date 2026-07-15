"""Archive/restore admin action button styling (#233)."""

from __future__ import annotations

import re
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app import admin_contacts, admin_research_pages
from app.admin_auth import SESSION_COOKIE_NAME
from app.admin_layout import admin_archive_action_button_class
from app.admin_preview import (
    PREVIEW_COMPANY_DETAIL_ACTIVE_ID,
    PREVIEW_COMPANY_DETAIL_ARCHIVED_ID,
    PREVIEW_CONTACT_DETAIL_ACTIVE_ID,
    PREVIEW_CONTACT_DETAIL_ARCHIVED_ID,
    PREVIEW_CONTACT_EDIT_ARCHIVED_ID,
    build_preview_company_research_detail,
    build_preview_contact_research_detail,
)
from app.main import app

ADMIN_CSS = Path(__file__).resolve().parents[1] / "site/assets/admin.css"

COMPANY_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
CONTACT_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

client = TestClient(app, follow_redirects=False)


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
    pattern = rf'<button class="([^"]+)" type="submit">{re.escape(label)}</button>'
    match = re.search(pattern, html)
    return match.group(1) if match else None


@pytest.mark.unit
def test_admin_archive_action_button_class_variants() -> None:
    assert admin_archive_action_button_class(archived=False) == (
        "admin-action-btn admin-action-btn--destructive"
    )
    assert admin_archive_action_button_class(archived=True) == (
        "admin-action-btn admin-action-btn--secondary"
    )


@pytest.mark.unit
def test_company_research_page_archive_and_restore_markup() -> None:
    active_html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme"},
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    active_classes = _archive_button_markup(active_html, "Archive company")
    assert active_classes == "admin-action-btn admin-action-btn--destructive"
    assert 'class="admin-exit" type="submit">Archive company' not in active_html

    archived_html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme", "archived_at": "2026-01-01"},
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    restore_classes = _archive_button_markup(archived_html, "Restore company")
    assert restore_classes == "admin-action-btn admin-action-btn--secondary"
    assert f'/admin/companies/{COMPANY_ID}/restore' in archived_html


@pytest.mark.unit
def test_contact_research_and_edit_archive_markup() -> None:
    detail_html = admin_research_pages.render_admin_contact_research_page(
        contact={"id": CONTACT_ID, "full_name": "Pat", "buying_roles": []},
        company=None,
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert (
        _archive_button_markup(detail_html, "Archive contact")
        == "admin-action-btn admin-action-btn--destructive"
    )

    edit_html = admin_contacts.render_contact_form_page(
        admin_username="operator",
        csrf_token="csrf",
        companies=[],
        contact={"id": CONTACT_ID, "full_name": "Pat", "archived_at": "2026-01-01"},
    )
    assert (
        _archive_button_markup(edit_html, "Restore contact")
        == "admin-action-btn admin-action-btn--secondary"
    )
    assert 'class="admin-exit" type="submit">' not in edit_html


@pytest.mark.unit
def test_admin_action_btn_css_resets_native_button_appearance() -> None:
    css = ADMIN_CSS.read_text(encoding="utf-8")
    base = _rule_block(css, ".admin-action-btn {")
    assert "appearance: none" in base
    assert "background:" in base
    assert "border:" in base
    assert "padding:" in base
    assert "cursor: pointer" in base
    assert "border-radius:" in base
    assert "font-family: inherit" in base
    assert "color:" in base

    assert ".admin-action-btn:focus-visible" in css
    assert ".admin-action-btn:active:not(:disabled)" in css
    assert ".admin-action-btn:disabled" in css
    assert ".admin-action-btn--destructive" in css
    assert ".admin-action-btn--destructive:hover:not(:disabled)" in css
    assert ".admin-action-btn--secondary" in css
    assert ".admin-action-btn--secondary:hover:not(:disabled)" in css

    disabled = _rule_block(css, ".admin-action-btn:disabled {")
    assert "cursor: not-allowed" in disabled
    assert "opacity:" in disabled


@pytest.mark.unit
def test_preview_company_and_contact_detail_archive_states() -> None:
    active = build_preview_company_research_detail(PREVIEW_COMPANY_DETAIL_ACTIVE_ID)
    assert active is not None
    company, contacts, records = active
    assert company.get("archived_at") is None
    assert contacts
    assert records

    archived = build_preview_company_research_detail(PREVIEW_COMPANY_DETAIL_ARCHIVED_ID)
    assert archived is not None
    assert archived[0].get("archived_at") is not None

    contact_active = build_preview_contact_research_detail(PREVIEW_CONTACT_DETAIL_ACTIVE_ID)
    assert contact_active is not None
    assert contact_active[0].get("archived_at") is None

    contact_archived = build_preview_contact_research_detail(PREVIEW_CONTACT_DETAIL_ARCHIVED_ID)
    assert contact_archived is not None
    assert contact_archived[0].get("archived_at") is not None


@pytest.mark.unit
@pytest.mark.integration
def test_preview_routes_render_themed_archive_buttons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "42")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cookie = {SESSION_COOKIE_NAME: "preview-screenshot-session"}

    company_archive = client.get(
        f"/admin/companies/{PREVIEW_COMPANY_DETAIL_ACTIVE_ID}",
        cookies=cookie,
    )
    assert company_archive.status_code == 200
    assert "admin-action-btn--destructive" in company_archive.text
    assert "Archive company" in company_archive.text

    company_restore = client.get(
        f"/admin/companies/{PREVIEW_COMPANY_DETAIL_ARCHIVED_ID}",
        cookies=cookie,
    )
    assert company_restore.status_code == 200
    assert "admin-action-btn--secondary" in company_restore.text
    assert "Restore company" in company_restore.text

    contact_archive = client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_DETAIL_ACTIVE_ID}",
        cookies=cookie,
    )
    assert contact_archive.status_code == 200
    assert "admin-action-btn--destructive" in contact_archive.text
    assert "Archive contact" in contact_archive.text

    contact_restore = client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_DETAIL_ARCHIVED_ID}",
        cookies=cookie,
    )
    assert contact_restore.status_code == 200
    assert "admin-action-btn--secondary" in contact_restore.text
    assert "Restore contact" in contact_restore.text

    contact_edit = client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_EDIT_ARCHIVED_ID}/edit",
        cookies=cookie,
    )
    assert contact_edit.status_code == 200
    assert "admin-action-btn--secondary" in contact_edit.text
    assert "Restore contact" in contact_edit.text
