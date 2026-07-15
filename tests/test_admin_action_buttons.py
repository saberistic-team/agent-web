"""Tests for Archive/Restore admin action button styling (#233)."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app import admin_contacts
from app.admin_layout import admin_archive_action_button_class
from app.admin_preview import (
    PREVIEW_CRM_COMPANY_ACTIVE_ID,
    PREVIEW_CRM_COMPANY_ARCHIVED_ID,
    PREVIEW_CRM_CONTACT_ACTIVE_ID,
    PREVIEW_CRM_CONTACT_ARCHIVED_ID,
)
from app.admin_research_pages import (
    render_admin_company_research_page,
    render_admin_contact_research_page,
)
from app.main import app

client = TestClient(app, follow_redirects=False)

ADMIN_CSS = Path(__file__).resolve().parents[1] / "site/assets/admin.css"
SITE_CSS = Path(__file__).resolve().parents[1] / "site/assets/site.css"

COMPANY_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
CONTACT_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


@pytest.mark.unit
def test_admin_archive_action_button_class_selects_modifier() -> None:
    assert admin_archive_action_button_class(is_archived=False) == (
        "admin-action admin-action--destructive"
    )
    assert admin_archive_action_button_class(is_archived=True) == (
        "admin-action admin-action--secondary"
    )


@pytest.mark.unit
def test_company_research_page_archive_and_restore_use_semantic_classes() -> None:
    active_html = render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme"},
        contacts=[],
        records=[],
        csrf_token="csrf",
    )
    assert 'class="admin-action admin-action--destructive" type="submit">Archive company' in active_html
    assert 'class="admin-exit" type="submit"' not in active_html

    archived_html = render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme", "archived_at": "2026-01-01"},
        contacts=[],
        records=[],
        csrf_token="csrf",
    )
    assert 'class="admin-action admin-action--secondary" type="submit">Restore company' in archived_html


@pytest.mark.unit
def test_contact_research_page_archive_and_restore_use_semantic_classes() -> None:
    active_html = render_admin_contact_research_page(
        contact={"id": CONTACT_ID, "full_name": "Pat", "buying_roles": []},
        company=None,
        records=[],
        csrf_token="csrf",
    )
    assert 'class="admin-action admin-action--destructive" type="submit">Archive contact' in active_html

    archived_html = render_admin_contact_research_page(
        contact={
            "id": CONTACT_ID,
            "full_name": "Pat",
            "buying_roles": [],
            "archived_at": "2026-01-01",
        },
        company=None,
        records=[],
        csrf_token="csrf",
    )
    assert 'class="admin-action admin-action--secondary" type="submit">Restore contact' in archived_html


@pytest.mark.unit
def test_contact_edit_page_archive_and_restore_use_semantic_classes() -> None:
    active_html = admin_contacts.render_contact_form_page(
        admin_username="operator",
        csrf_token="csrf",
        companies=[],
        contact={"id": CONTACT_ID, "full_name": "Pat"},
    )
    assert 'class="admin-action admin-action--destructive" type="submit">Archive contact' in active_html

    archived_html = admin_contacts.render_contact_form_page(
        admin_username="operator",
        csrf_token="csrf",
        companies=[],
        contact={"id": CONTACT_ID, "full_name": "Pat", "archived_at": "2026-01-01"},
    )
    assert 'class="admin-action admin-action--secondary" type="submit">Restore contact' in archived_html


@pytest.mark.unit
def test_admin_action_css_resets_native_button_appearance() -> None:
    css = ADMIN_CSS.read_text(encoding="utf-8")
    base_block = css.split(".admin-action {", 1)[1].split("}", 1)[0]
    assert "appearance: none" in base_block
    assert "-webkit-appearance: none" in base_block
    assert "cursor: pointer" in base_block
    assert "border-radius:" in base_block
    assert "padding:" in base_block
    assert "font-family: inherit" in base_block
    assert "background:" in base_block
    assert "border:" in base_block

    destructive_block = css.split(".admin-action--destructive {", 1)[1].split("}", 1)[0]
    secondary_block = css.split(".admin-action--secondary {", 1)[1].split("}", 1)[0]
    assert "background:" in destructive_block
    assert "border-color:" in destructive_block
    assert "background:" in secondary_block
    assert "border-color:" in secondary_block

    assert ".admin-action--destructive:hover," in css
    assert ".admin-action--destructive:focus-visible" in css
    assert ".admin-action--destructive:active:not(:disabled)" in css
    assert ".admin-action--destructive:disabled" in css
    assert ".admin-action--secondary:hover," in css
    assert ".admin-action--secondary:focus-visible" in css
    assert ".admin-action--secondary:active:not(:disabled)" in css
    assert ".admin-action--secondary:disabled" in css


@pytest.mark.unit
def test_admin_action_css_is_separate_from_admin_exit() -> None:
    css = ADMIN_CSS.read_text(encoding="utf-8")
    exit_block = css.split(".admin-exit {", 1)[1].split("}", 1)[0]
    assert "background:" not in exit_block
    assert ".admin-action {" in css


@pytest.mark.unit
def test_primary_save_actions_remain_distinct_from_archive_actions() -> None:
    site_css = SITE_CSS.read_text(encoding="utf-8")
    admin_css = ADMIN_CSS.read_text(encoding="utf-8")
    cta_block = site_css.split(".cta {", 1)[1].split("}", 1)[0]
    destructive_block = admin_css.split(".admin-action--destructive {", 1)[1].split("}", 1)[0]
    assert "var(--accent)" in cta_block
    assert "var(--accent)" not in destructive_block


@pytest.mark.unit
@pytest.mark.integration
def test_preview_company_detail_pages_render_archive_and_restore_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "233")
    monkeypatch.setenv("ADMIN_USERNAME", "preview-admin")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", PasswordHasher().hash("preview"))
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "preview-session-secret-32chars-minimum")
    monkeypatch.setenv("BASE_URL", "http://127.0.0.1:8765")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    archive_page = client.get(f"/admin/companies/{PREVIEW_CRM_COMPANY_ACTIVE_ID}")
    assert archive_page.status_code == 200
    assert 'class="admin-action admin-action--destructive" type="submit">Archive company' in archive_page.text
    assert "No research records yet." not in archive_page.text

    restore_page = client.get(f"/admin/companies/{PREVIEW_CRM_COMPANY_ARCHIVED_ID}")
    assert restore_page.status_code == 200
    assert 'class="admin-action admin-action--secondary" type="submit">Restore company' in restore_page.text


@pytest.mark.unit
@pytest.mark.integration
def test_preview_contact_detail_and_edit_render_archive_and_restore_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "233")
    monkeypatch.setenv("ADMIN_USERNAME", "preview-admin")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", PasswordHasher().hash("preview"))
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "preview-session-secret-32chars-minimum")
    monkeypatch.setenv("BASE_URL", "http://127.0.0.1:8765")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    archive_detail = client.get(f"/admin/contacts/{PREVIEW_CRM_CONTACT_ACTIVE_ID}")
    assert archive_detail.status_code == 200
    assert (
        'class="admin-action admin-action--destructive" type="submit">Archive contact'
        in archive_detail.text
    )

    restore_detail = client.get(f"/admin/contacts/{PREVIEW_CRM_CONTACT_ARCHIVED_ID}")
    assert restore_detail.status_code == 200
    assert (
        'class="admin-action admin-action--secondary" type="submit">Restore contact'
        in restore_detail.text
    )

    archive_edit = client.get(f"/admin/contacts/{PREVIEW_CRM_CONTACT_ACTIVE_ID}/edit")
    assert archive_edit.status_code == 200
    assert (
        'class="admin-action admin-action--destructive" type="submit">Archive contact'
        in archive_edit.text
    )

    restore_edit = client.get(f"/admin/contacts/{PREVIEW_CRM_CONTACT_ARCHIVED_ID}/edit")
    assert restore_edit.status_code == 200
    assert (
        'class="admin-action admin-action--secondary" type="submit">Restore contact'
        in restore_edit.text
    )
