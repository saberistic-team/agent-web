"""Archive/restore admin action button styling (#233)."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app import admin_contacts, admin_research_pages
from app.admin_auth import SESSION_COOKIE_NAME
from app.admin_layout import archive_action_button_class
from app.admin_preview import (
    PREVIEW_COMPANY_DETAIL_ACTIVE_ID,
    PREVIEW_COMPANY_DETAIL_ARCHIVED_ID,
    PREVIEW_CONTACT_DETAIL_ACTIVE_ID,
    PREVIEW_CONTACT_DETAIL_ARCHIVED_ID,
    build_preview_company_detail,
    build_preview_contact_detail,
)
from app.main import app

ADMIN_CSS = Path(__file__).resolve().parents[1] / "site/assets/admin.css"
COMPANY_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
CONTACT_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


def _admin_action_css_block() -> str:
    return ADMIN_CSS.read_text(encoding="utf-8").split(".admin-action {", 1)[1].split("}", 1)[0]


@pytest.mark.unit
def test_archive_action_button_class_maps_archived_state() -> None:
    assert archive_action_button_class(is_archived=False) == (
        "admin-action admin-action--destructive"
    )
    assert archive_action_button_class(is_archived=True) == (
        "admin-action admin-action--secondary"
    )


@pytest.mark.unit
def test_admin_css_action_buttons_reset_native_appearance() -> None:
    css = ADMIN_CSS.read_text(encoding="utf-8")
    action_block = _admin_action_css_block()
    assert "background:" in action_block
    assert "border:" in action_block
    assert "padding:" in action_block
    assert "font-family: inherit" in action_block
    assert "cursor: pointer" in action_block
    assert "border-radius:" in action_block
    assert ".admin-action:hover" in css
    assert ".admin-action:focus-visible" in css
    assert ".admin-action:active:not(:disabled)" in css
    assert ".admin-action:disabled" in css
    assert ".admin-action--destructive" in css
    assert ".admin-action--secondary" in css
    destructive_block = css.split(".admin-action--destructive {", 1)[1].split("}", 1)[0]
    assert "background:" in destructive_block
    assert "white" not in destructive_block.lower()
    assert "#fff" not in destructive_block.lower()


@pytest.mark.unit
def test_company_research_archive_and_restore_use_semantic_action_classes() -> None:
    archive_html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme"},
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert 'class="admin-action admin-action--destructive"' in archive_html
    assert "Archive company" in archive_html
    assert 'class="admin-exit" type="submit">Archive company' not in archive_html

    restore_html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme", "archived_at": "2026-01-01"},
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert 'class="admin-action admin-action--secondary"' in restore_html
    assert "Restore company" in restore_html


@pytest.mark.unit
def test_contact_research_and_edit_archive_actions_use_semantic_classes() -> None:
    contact = {
        "id": CONTACT_ID,
        "full_name": "Pat Example",
        "buying_roles": [],
    }
    detail_archive = admin_research_pages.render_admin_contact_research_page(
        contact=contact,
        company=None,
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert 'class="admin-action admin-action--destructive"' in detail_archive
    assert "Archive contact" in detail_archive

    detail_restore = admin_research_pages.render_admin_contact_research_page(
        contact={**contact, "archived_at": "2026-01-01"},
        company=None,
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert 'class="admin-action admin-action--secondary"' in detail_restore
    assert "Restore contact" in detail_restore

    edit_archive = admin_contacts.render_contact_form_page(
        admin_username="operator",
        csrf_token="csrf",
        companies=[],
        contact=contact,
    )
    assert 'class="admin-action admin-action--destructive"' in edit_archive
    assert "Archive contact" in edit_archive

    edit_restore = admin_contacts.render_contact_form_page(
        admin_username="operator",
        csrf_token="csrf",
        companies=[],
        contact={**contact, "archived_at": "2026-01-01"},
    )
    assert 'class="admin-action admin-action--secondary"' in edit_restore
    assert "Restore contact" in edit_restore


@pytest.mark.unit
def test_preview_company_and_contact_detail_stable_with_seed() -> None:
    import random

    active_company = build_preview_company_detail(
        PREVIEW_COMPANY_DETAIL_ACTIVE_ID, rng=random.Random(42)
    )
    archived_company = build_preview_company_detail(
        PREVIEW_COMPANY_DETAIL_ARCHIVED_ID, rng=random.Random(42)
    )
    active_contact = build_preview_contact_detail(
        PREVIEW_CONTACT_DETAIL_ACTIVE_ID, rng=random.Random(42)
    )
    archived_contact = build_preview_contact_detail(
        PREVIEW_CONTACT_DETAIL_ARCHIVED_ID, rng=random.Random(42)
    )
    assert active_company is not None
    assert archived_company is not None
    assert active_contact is not None
    assert archived_contact is not None
    assert active_company["archived_at"] is None
    assert archived_company["archived_at"] is not None
    assert active_contact["archived_at"] is None
    assert archived_contact["archived_at"] is not None
    repeat = build_preview_company_detail(
        PREVIEW_COMPANY_DETAIL_ACTIVE_ID, rng=random.Random(42)
    )
    assert repeat == active_company


@pytest.mark.unit
@pytest.mark.integration
def test_preview_routes_render_archive_and_restore_action_buttons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from argon2 import PasswordHasher

    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "42")
    monkeypatch.setenv("ADMIN_USERNAME", "preview-admin")
    monkeypatch.setenv(
        "ADMIN_PASSWORD_HASH",
        PasswordHasher().hash("preview"),
    )
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "preview-session-secret-32chars-minimum")
    monkeypatch.setenv("BASE_URL", "http://127.0.0.1:8765")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    client = TestClient(app, follow_redirects=False)
    cookies = {SESSION_COOKIE_NAME: "preview-screenshot-session"}

    active_company = client.get(
        f"/admin/companies/{PREVIEW_COMPANY_DETAIL_ACTIVE_ID}",
        cookies=cookies,
    )
    assert active_company.status_code == 200
    assert 'class="admin-action admin-action--destructive"' in active_company.text
    assert "Archive company" in active_company.text

    archived_company = client.get(
        f"/admin/companies/{PREVIEW_COMPANY_DETAIL_ARCHIVED_ID}",
        cookies=cookies,
    )
    assert archived_company.status_code == 200
    assert 'class="admin-action admin-action--secondary"' in archived_company.text
    assert "Restore company" in archived_company.text

    active_contact = client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_DETAIL_ACTIVE_ID}",
        cookies=cookies,
    )
    assert active_contact.status_code == 200
    assert 'class="admin-action admin-action--destructive"' in active_contact.text
    assert "Archive contact" in active_contact.text

    archived_contact = client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_DETAIL_ARCHIVED_ID}",
        cookies=cookies,
    )
    assert archived_contact.status_code == 200
    assert 'class="admin-action admin-action--secondary"' in archived_contact.text
    assert "Restore contact" in archived_contact.text

    archived_edit = client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_DETAIL_ARCHIVED_ID}/edit",
        cookies=cookies,
    )
    assert archived_edit.status_code == 200
    assert 'class="admin-action admin-action--secondary"' in archived_edit.text
    assert "Restore contact" in archived_edit.text
