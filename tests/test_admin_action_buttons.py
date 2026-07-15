"""Regression tests for themed Archive/Restore admin action buttons (#233)."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.admin_auth import SESSION_COOKIE_NAME
from app.admin_contacts import render_contact_form_page
from app.admin_layout import render_admin_archive_action_button
from app.admin_preview import (
    PREVIEW_COMPANY_RESEARCH_ACTIVE_ID,
    PREVIEW_COMPANY_RESEARCH_ARCHIVED_ID,
    PREVIEW_CONTACT_RESEARCH_ACTIVE_ID,
    PREVIEW_CONTACT_RESEARCH_ARCHIVED_ID,
)
from app.admin_research_pages import (
    render_admin_company_research_page,
    render_admin_contact_research_page,
)
from app.main import app

client = TestClient(app, follow_redirects=False)

ADMIN_CSS = Path(__file__).resolve().parents[1] / "site/assets/admin.css"
COMPANY_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
CONTACT_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


@pytest.mark.unit
def test_render_admin_archive_action_button_uses_semantic_classes() -> None:
    archive = render_admin_archive_action_button(label="Archive company", archived=False)
    restore = render_admin_archive_action_button(label="Restore company", archived=True)
    disabled = render_admin_archive_action_button(
        label="Archive company",
        archived=False,
        disabled=True,
    )
    assert 'class="admin-action-btn admin-action-btn--destructive"' in archive
    assert 'class="admin-action-btn admin-action-btn--secondary"' in restore
    assert "admin-exit" not in archive
    assert "admin-exit" not in restore
    assert " disabled" in disabled


@pytest.mark.unit
def test_company_research_page_archive_and_restore_markup() -> None:
    company = {"id": COMPANY_ID, "name": "Acme"}
    archive_html = render_admin_company_research_page(
        company=company,
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert 'class="admin-action-btn admin-action-btn--destructive"' in archive_html
    assert "Archive company" in archive_html
    assert 'action="/admin/companies/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/archive"' in archive_html

    restore_html = render_admin_company_research_page(
        company={**company, "archived_at": "2026-01-01"},
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert 'class="admin-action-btn admin-action-btn--secondary"' in restore_html
    assert "Restore company" in restore_html


@pytest.mark.unit
def test_contact_research_and_edit_pages_archive_and_restore_markup() -> None:
    contact = {"id": CONTACT_ID, "full_name": "Pat", "buying_roles": []}
    research_archive = render_admin_contact_research_page(
        contact=contact,
        company=None,
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert 'class="admin-action-btn admin-action-btn--destructive"' in research_archive
    assert "Archive contact" in research_archive

    research_restore = render_admin_contact_research_page(
        contact={**contact, "archived_at": "2026-01-01"},
        company=None,
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert 'class="admin-action-btn admin-action-btn--secondary"' in research_restore
    assert "Restore contact" in research_restore

    edit_archive = render_contact_form_page(
        csrf_token="csrf",
        admin_username="operator",
        companies=[],
        contact=contact,
    )
    assert 'class="admin-action-btn admin-action-btn--destructive"' in edit_archive

    edit_restore = render_contact_form_page(
        csrf_token="csrf",
        admin_username="operator",
        companies=[],
        contact={**contact, "archived_at": "2026-01-01"},
    )
    assert 'class="admin-action-btn admin-action-btn--secondary"' in edit_restore


@pytest.mark.unit
def test_admin_action_button_css_resets_native_button_appearance() -> None:
    css = ADMIN_CSS.read_text(encoding="utf-8")
    base_block = css.split(".admin-action-btn {", 1)[1].split("}", 1)[0]
    assert "appearance: none" in base_block
    assert "-webkit-appearance: none" in base_block
    assert "background:" in base_block
    assert "border:" in base_block
    assert "padding:" in base_block
    assert "font-family: inherit" in base_block
    assert "cursor: pointer" in base_block
    assert "border-radius:" in base_block
    assert ":hover:not(:disabled)" in css
    assert ".admin-action-btn:focus-visible" in css
    assert ":active:not(:disabled)" in css
    assert ".admin-action-btn:disabled" in css
    assert ".admin-action-btn--destructive" in css
    assert ".admin-action-btn--secondary" in css
    assert "outline:" in css.split(".admin-action-btn:focus-visible", 1)[1].split("}", 1)[0]


@pytest.mark.unit
def test_admin_action_buttons_do_not_use_primary_cta_classes() -> None:
    html = render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme"},
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    archive_start = html.index('class="admin-action-btn admin-action-btn--destructive"')
    archive_snippet = html[archive_start : archive_start + 120]
    assert "cta admin-submit" not in archive_snippet
    assert "admin-exit" not in archive_snippet


@pytest.mark.unit
@pytest.mark.integration
def test_preview_company_and_contact_detail_pages_render_archive_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "42")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cookie = {SESSION_COOKIE_NAME: "preview-screenshot-session"}

    archive_company = client.get(
        f"/admin/companies/{PREVIEW_COMPANY_RESEARCH_ACTIVE_ID}",
        cookies=cookie,
    )
    assert archive_company.status_code == 200
    assert 'class="admin-action-btn admin-action-btn--destructive"' in archive_company.text
    assert "Archive company" in archive_company.text

    restore_company = client.get(
        f"/admin/companies/{PREVIEW_COMPANY_RESEARCH_ARCHIVED_ID}",
        cookies=cookie,
    )
    assert restore_company.status_code == 200
    assert 'class="admin-action-btn admin-action-btn--secondary"' in restore_company.text
    assert "Restore company" in restore_company.text

    archive_contact = client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_RESEARCH_ACTIVE_ID}",
        cookies=cookie,
    )
    assert archive_contact.status_code == 200
    assert 'class="admin-action-btn admin-action-btn--destructive"' in archive_contact.text

    restore_contact = client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_RESEARCH_ARCHIVED_ID}",
        cookies=cookie,
    )
    assert restore_contact.status_code == 200
    assert 'class="admin-action-btn admin-action-btn--secondary"' in restore_contact.text

    edit_archive = client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_RESEARCH_ACTIVE_ID}/edit",
        cookies=cookie,
    )
    assert edit_archive.status_code == 200
    assert 'class="admin-action-btn admin-action-btn--destructive"' in edit_archive.text

    edit_restore = client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_RESEARCH_ARCHIVED_ID}/edit",
        cookies=cookie,
    )
    assert edit_restore.status_code == 200
    assert 'class="admin-action-btn admin-action-btn--secondary"' in edit_restore.text
