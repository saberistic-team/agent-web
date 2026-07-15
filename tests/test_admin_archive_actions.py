"""Archive/Restore admin action button styling and preview coverage (#233)."""

from __future__ import annotations

import random
from pathlib import Path
from uuid import UUID

import pytest

from app import admin_contacts, admin_research_pages
from app.admin_layout import render_admin_archive_action_button
from app.admin_preview import (
    PREVIEW_CRM_COMPANY_ACTIVE_ID,
    PREVIEW_CRM_COMPANY_ARCHIVED_ID,
    PREVIEW_CRM_CONTACT_ACTIVE_ID,
    PREVIEW_CRM_CONTACT_ARCHIVED_ID,
    preview_crm_company_detail,
    preview_crm_contact_detail,
    preview_crm_contact_edit,
)

ADMIN_CSS = Path(__file__).resolve().parents[1] / "site/assets/admin.css"
COMPANY_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
CONTACT_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


@pytest.mark.unit
def test_render_admin_archive_action_button_variants() -> None:
    archive = render_admin_archive_action_button(label="Archive company", is_archived=False)
    restore = render_admin_archive_action_button(label="Restore company", is_archived=True)

    assert 'class="admin-action-btn admin-action-btn--destructive"' in archive
    assert "Archive company" in archive
    assert 'class="admin-action-btn admin-action-btn--restore"' in restore
    assert "Restore company" in restore
    assert "admin-exit" not in archive
    assert "admin-exit" not in restore


@pytest.mark.unit
def test_company_research_page_uses_themed_archive_and_restore_buttons() -> None:
    archive_html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme"},
        contacts=[],
        records=[],
        csrf_token="csrf",
    )
    assert 'class="admin-action-btn admin-action-btn--destructive"' in archive_html
    assert "Archive company" in archive_html
    assert 'class="admin-exit" type="submit"' not in archive_html

    restore_html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme", "archived_at": "2026-01-01"},
        contacts=[],
        records=[],
        csrf_token="csrf",
    )
    assert 'class="admin-action-btn admin-action-btn--restore"' in restore_html
    assert "Restore company" in restore_html


@pytest.mark.unit
def test_contact_research_and_edit_pages_use_themed_archive_buttons() -> None:
    contact = {"id": CONTACT_ID, "full_name": "Pat", "buying_roles": []}
    detail_html = admin_research_pages.render_admin_contact_research_page(
        contact=contact,
        company=None,
        records=[],
        csrf_token="csrf",
    )
    assert 'class="admin-action-btn admin-action-btn--destructive"' in detail_html
    assert "Archive contact" in detail_html

    edit_html = admin_contacts.render_contact_form_page(
        admin_username="operator",
        csrf_token="csrf",
        companies=[],
        contact=contact,
    )
    assert 'class="admin-action-btn admin-action-btn--destructive"' in edit_html
    assert "Archive contact" in edit_html

    archived_contact = {**contact, "archived_at": "2026-01-01"}
    restore_detail = admin_research_pages.render_admin_contact_research_page(
        contact=archived_contact,
        company=None,
        records=[],
        csrf_token="csrf",
    )
    assert 'class="admin-action-btn admin-action-btn--restore"' in restore_detail
    assert "Restore contact" in restore_detail


@pytest.mark.unit
def test_admin_css_action_buttons_reset_native_appearance() -> None:
    css = ADMIN_CSS.read_text(encoding="utf-8")
    base_block = css.split(".admin-action-btn {", 1)[1].split("}", 1)[0]
    destructive_block = css.split(".admin-action-btn--destructive {", 1)[1].split("}", 1)[0]
    restore_block = css.split(".admin-action-btn--restore {", 1)[1].split("}", 1)[0]

    for block in (base_block, destructive_block, restore_block):
        assert "background:" in block

    assert "border:" in base_block
    assert "border-color:" in destructive_block
    assert "border-color:" in restore_block
    assert "padding:" in base_block
    assert "cursor: pointer" in base_block

    assert ".admin-action-btn:focus-visible" in css
    assert ".admin-action-btn:disabled" in css
    assert ".admin-action-btn:active:not(:disabled)" in css
    assert ".admin-action-btn--destructive:hover:not(:disabled)" in css
    assert ".admin-action-btn--restore:hover:not(:disabled)" in css
    assert "appearance:" not in base_block


@pytest.mark.unit
def test_preview_crm_detail_states_are_stable_with_seed() -> None:
    rng = random.Random(19)
    active_company, active_contacts, active_records = preview_crm_company_detail(
        PREVIEW_CRM_COMPANY_ACTIVE_ID,
        rng=rng,
    )
    assert active_company is not None
    assert active_company["archived_at"] is None
    assert active_contacts
    assert active_records

    archived_company, _, _ = preview_crm_company_detail(
        PREVIEW_CRM_COMPANY_ARCHIVED_ID,
        rng=rng,
    )
    assert archived_company is not None
    assert archived_company["archived_at"] is not None

    active_contact, company, records = preview_crm_contact_detail(
        PREVIEW_CRM_CONTACT_ACTIVE_ID,
        rng=rng,
    )
    assert active_contact is not None
    assert active_contact["archived_at"] is None
    assert company is not None
    assert records

    archived_contact, _, _ = preview_crm_contact_detail(
        PREVIEW_CRM_CONTACT_ARCHIVED_ID,
        rng=rng,
    )
    assert archived_contact is not None
    assert archived_contact["archived_at"] is not None

    edit_contact, companies = preview_crm_contact_edit(PREVIEW_CRM_CONTACT_ACTIVE_ID, rng=rng)
    assert edit_contact is not None
    assert companies


@pytest.mark.unit
@pytest.mark.integration
def test_preview_company_and_contact_detail_render_archive_action_classes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi.testclient import TestClient

    from app.admin_auth import SESSION_COOKIE_NAME
    from app.main import app

    client = TestClient(app)

    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "19")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    archive_company = client.get(
        f"/admin/companies/{PREVIEW_CRM_COMPANY_ACTIVE_ID}",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert archive_company.status_code == 200
    assert 'class="admin-action-btn admin-action-btn--destructive"' in archive_company.text
    assert "Archive company" in archive_company.text

    restore_company = client.get(
        f"/admin/companies/{PREVIEW_CRM_COMPANY_ARCHIVED_ID}",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert restore_company.status_code == 200
    assert 'class="admin-action-btn admin-action-btn--restore"' in restore_company.text

    archive_contact = client.get(
        f"/admin/contacts/{PREVIEW_CRM_CONTACT_ACTIVE_ID}",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert archive_contact.status_code == 200
    assert 'class="admin-action-btn admin-action-btn--destructive"' in archive_contact.text

    restore_contact = client.get(
        f"/admin/contacts/{PREVIEW_CRM_CONTACT_ARCHIVED_ID}",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert restore_contact.status_code == 200
    assert 'class="admin-action-btn admin-action-btn--restore"' in restore_contact.text

    edit_contact = client.get(
        f"/admin/contacts/{PREVIEW_CRM_CONTACT_ACTIVE_ID}/edit",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert edit_contact.status_code == 200
    assert 'class="admin-action-btn admin-action-btn--destructive"' in edit_contact.text
