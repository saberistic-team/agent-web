"""Archive/restore admin action button styling (#233)."""

from __future__ import annotations

import random
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.admin_auth import SESSION_COOKIE_NAME
from app.admin_contacts import render_contact_form_page
from app.admin_preview import (
    PREVIEW_COMPANY_ACTIVE_ID,
    PREVIEW_COMPANY_ARCHIVED_ID,
    PREVIEW_CONTACT_ACTIVE_ID,
    PREVIEW_CONTACT_ARCHIVED_ID,
    preview_company_research_detail,
    preview_contact_edit_detail,
    preview_contact_research_detail,
)
from app.admin_research_pages import (
    render_admin_company_research_page,
    render_admin_contact_research_page,
)
from app.main import app

ADMIN_CSS = Path(__file__).resolve().parents[1] / "site/assets/admin.css"
client = TestClient(app, follow_redirects=False)

COMPANY_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
CONTACT_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


@pytest.mark.unit
def test_archive_buttons_use_semantic_action_classes_not_admin_exit() -> None:
    company_html = render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme"},
        contacts=[],
        records=[],
        csrf_token="csrf",
    )
    assert 'class="admin-action-btn admin-action-btn--destructive"' in company_html
    assert "Archive company" in company_html
    assert 'class="admin-exit" type="submit"' not in company_html

    restored_company_html = render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme", "archived_at": "2026-01-01"},
        contacts=[],
        records=[],
        csrf_token="csrf",
    )
    assert 'class="admin-action-btn admin-action-btn--restore"' in restored_company_html
    assert "Restore company" in restored_company_html

    contact_html = render_admin_contact_research_page(
        contact={"id": CONTACT_ID, "full_name": "Pat", "buying_roles": []},
        company=None,
        records=[],
        csrf_token="csrf",
    )
    assert 'class="admin-action-btn admin-action-btn--destructive"' in contact_html

    edit_html = render_contact_form_page(
        admin_username="operator",
        csrf_token="csrf",
        companies=[],
        contact={"id": CONTACT_ID, "full_name": "Pat"},
    )
    assert 'class="admin-action-btn admin-action-btn--destructive"' in edit_html
    assert 'class="admin-exit" type="submit"' not in edit_html


@pytest.mark.unit
def test_admin_action_btn_css_resets_native_button_appearance() -> None:
    css = ADMIN_CSS.read_text(encoding="utf-8")
    base_block = css.split(".admin-action-btn {", 1)[1].split("}", 1)[0]
    assert "appearance: none" in base_block
    assert "background:" in base_block
    assert "border:" in base_block
    assert "padding:" in base_block
    assert "font-family: inherit" in base_block
    assert "cursor: pointer" in base_block
    assert "border-radius:" in base_block

    destructive_block = css.split(".admin-action-btn--destructive {", 1)[1].split("}", 1)[0]
    assert "background:" in destructive_block
    assert "#e05a5a" in destructive_block or "#ffb4b4" in destructive_block

    restore_block = css.split(".admin-action-btn--restore {", 1)[1].split("}", 1)[0]
    assert "background:" in restore_block
    assert "var(--accent)" in restore_block or "var(--ink)" in restore_block

    assert ".admin-action-btn:focus-visible" in css
    assert ".admin-action-btn:disabled" in css
    assert ".admin-action-btn:active:not(:disabled)" in css
    assert ".admin-action-btn--destructive:hover" in css
    assert ".admin-action-btn--restore:hover" in css


@pytest.mark.unit
def test_preview_company_and_contact_detail_archive_states(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "42")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    active_company = client.get(
        f"/admin/companies/{PREVIEW_COMPANY_ACTIVE_ID}",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert active_company.status_code == 200
    assert 'class="admin-action-btn admin-action-btn--destructive"' in active_company.text
    assert "Archive company" in active_company.text

    archived_company = client.get(
        f"/admin/companies/{PREVIEW_COMPANY_ARCHIVED_ID}",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert archived_company.status_code == 200
    assert 'class="admin-action-btn admin-action-btn--restore"' in archived_company.text
    assert "Restore company" in archived_company.text

    active_contact = client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_ACTIVE_ID}",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert active_contact.status_code == 200
    assert 'class="admin-action-btn admin-action-btn--destructive"' in active_contact.text

    archived_contact = client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_ARCHIVED_ID}",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert archived_contact.status_code == 200
    assert 'class="admin-action-btn admin-action-btn--restore"' in archived_contact.text

    active_edit = client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_ACTIVE_ID}/edit",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert active_edit.status_code == 200
    assert 'class="admin-action-btn admin-action-btn--destructive"' in active_edit.text

    archived_edit = client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_ARCHIVED_ID}/edit",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert archived_edit.status_code == 200
    assert 'class="admin-action-btn admin-action-btn--restore"' in archived_edit.text


@pytest.mark.unit
def test_preview_archive_detail_fixtures_stable_with_seed() -> None:
    a_company, a_contacts, a_records = preview_company_research_detail(
        archived=False, rng=random.Random(42)
    )
    b_company, b_contacts, b_records = preview_company_research_detail(
        archived=False, rng=random.Random(42)
    )
    assert a_company["name"] == b_company["name"]
    assert a_contacts[0]["full_name"] == b_contacts[0]["full_name"]
    assert a_records[0]["body"] == b_records[0]["body"]

    a_contact, a_co, a_recs = preview_contact_research_detail(
        archived=True, rng=random.Random(7)
    )
    b_contact, b_co, b_recs = preview_contact_research_detail(
        archived=True, rng=random.Random(7)
    )
    assert a_contact["full_name"] == b_contact["full_name"]
    assert a_co["name"] == b_co["name"]

    a_edit, a_companies = preview_contact_edit_detail(archived=False, rng=random.Random(3))
    b_edit, b_companies = preview_contact_edit_detail(archived=False, rng=random.Random(3))
    assert a_edit["full_name"] == b_edit["full_name"]
    assert a_companies[0]["name"] == b_companies[0]["name"]
