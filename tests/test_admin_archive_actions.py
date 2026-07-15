"""Archive/restore admin action button styling (#233)."""

from __future__ import annotations

import random
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app import admin_contacts, admin_research_pages
from app.admin_auth import SESSION_COOKIE_NAME
from app.admin_layout import archive_action_button_class
from app.admin_preview import (
    PREVIEW_CRM_COMPANY_ACTIVE_ID,
    PREVIEW_CRM_COMPANY_ARCHIVED_ID,
    PREVIEW_CRM_CONTACT_ACTIVE_ID,
    PREVIEW_CRM_CONTACT_ARCHIVED_ID,
    preview_crm_company,
    preview_crm_contact,
)
from app.main import app

ADMIN_CSS = Path(__file__).resolve().parents[1] / "site/assets/admin.css"

COMPANY_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
CONTACT_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

client = TestClient(app, follow_redirects=False)


def _admin_action_css_block(selector: str) -> str:
    css = ADMIN_CSS.read_text(encoding="utf-8")
    marker = f"{selector} {{"
    start = css.index(marker) + len(marker)
    end = css.index("}", start)
    return css[start:end]


@pytest.mark.unit
def test_archive_action_button_class_semantics() -> None:
    assert archive_action_button_class(archived=False) == (
        "admin-action admin-action--destructive"
    )
    assert archive_action_button_class(archived=True) == "admin-action admin-action--restore"


@pytest.mark.unit
def test_admin_action_css_resets_native_button_appearance() -> None:
    base = _admin_action_css_block(".admin-action")
    assert "background:" in base
    assert "border:" in base
    assert "padding:" in base
    assert "cursor: pointer" in base
    assert "border-radius:" in base
    assert "font-family: inherit" in base
    assert "color:" in base
    assert base.strip() != "background: none"


@pytest.mark.unit
def test_admin_action_css_includes_interaction_and_disabled_states() -> None:
    css = ADMIN_CSS.read_text(encoding="utf-8")
    for selector in (
        ".admin-action:hover",
        ".admin-action:focus-visible",
        ".admin-action:active",
        ".admin-action:disabled",
        ".admin-action--destructive:hover",
        ".admin-action--destructive:focus-visible",
        ".admin-action--restore:hover",
        ".admin-action--restore:focus-visible",
    ):
        assert selector in css


@pytest.mark.unit
def test_admin_action_destructive_and_restore_are_visually_distinct() -> None:
    destructive = _admin_action_css_block(".admin-action--destructive")
    restore = _admin_action_css_block(".admin-action--restore")
    assert destructive != restore
    assert "#e05a5a" in destructive or "#ffc8c8" in destructive
    assert "var(--accent)" in restore


@pytest.mark.unit
def test_company_research_page_archive_button_uses_semantic_action_class() -> None:
    html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme"},
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert 'class="admin-action admin-action--destructive" type="submit">Archive company' in html
    assert 'class="admin-exit" type="submit">Archive company' not in html


@pytest.mark.unit
def test_company_research_page_restore_button_uses_semantic_action_class() -> None:
    html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme", "archived_at": "2026-01-01"},
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert 'class="admin-action admin-action--restore" type="submit">Restore company' in html


@pytest.mark.unit
def test_contact_research_page_archive_and_restore_button_classes() -> None:
    archive_html = admin_research_pages.render_admin_contact_research_page(
        contact={"id": CONTACT_ID, "full_name": "Pat", "buying_roles": []},
        company=None,
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert 'class="admin-action admin-action--destructive" type="submit">Archive contact' in archive_html

    restore_html = admin_research_pages.render_admin_contact_research_page(
        contact={
            "id": CONTACT_ID,
            "full_name": "Pat",
            "buying_roles": [],
            "archived_at": "2026-01-01",
        },
        company=None,
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert 'class="admin-action admin-action--restore" type="submit">Restore contact' in restore_html


@pytest.mark.unit
def test_contact_edit_page_archive_and_restore_button_classes() -> None:
    archive_html = admin_contacts.render_contact_form_page(
        admin_username="operator",
        csrf_token="csrf",
        companies=[],
        contact={"id": CONTACT_ID, "full_name": "Pat"},
    )
    assert 'class="admin-action admin-action--destructive" type="submit">Archive contact' in archive_html

    restore_html = admin_contacts.render_contact_form_page(
        admin_username="operator",
        csrf_token="csrf",
        companies=[],
        contact={"id": CONTACT_ID, "full_name": "Pat", "archived_at": "2026-01-01"},
    )
    assert 'class="admin-action admin-action--restore" type="submit">Restore contact' in restore_html


@pytest.mark.unit
def test_preview_crm_company_and_contact_states_stable_with_seed() -> None:
    now = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)
    active_company = preview_crm_company(
        PREVIEW_CRM_COMPANY_ACTIVE_ID, rng=random.Random(42), now=now
    )
    archived_company = preview_crm_company(
        PREVIEW_CRM_COMPANY_ARCHIVED_ID, rng=random.Random(42), now=now
    )
    active_contact = preview_crm_contact(
        PREVIEW_CRM_CONTACT_ACTIVE_ID, rng=random.Random(42), now=now
    )
    archived_contact = preview_crm_contact(
        PREVIEW_CRM_CONTACT_ARCHIVED_ID, rng=random.Random(42), now=now
    )
    assert active_company is not None and active_company["archived_at"] is None
    assert archived_company is not None and archived_company["archived_at"] is not None
    assert active_contact is not None and active_contact["archived_at"] is None
    assert archived_contact is not None and archived_contact["archived_at"] is not None
    assert preview_crm_company(
        PREVIEW_CRM_COMPANY_ACTIVE_ID, rng=random.Random(42), now=now
    ) == active_company
    assert preview_crm_contact(
        PREVIEW_CRM_CONTACT_ARCHIVED_ID, rng=random.Random(42), now=now
    ) == archived_contact


@pytest.mark.unit
@pytest.mark.integration
def test_preview_company_detail_routes_render_archive_and_restore_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "42")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    archive_response = client.get(
        f"/admin/companies/{PREVIEW_CRM_COMPANY_ACTIVE_ID}",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    restore_response = client.get(
        f"/admin/companies/{PREVIEW_CRM_COMPANY_ARCHIVED_ID}",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert archive_response.status_code == 200
    assert restore_response.status_code == 200
    assert 'class="admin-action admin-action--destructive" type="submit">Archive company' in (
        archive_response.text
    )
    assert 'class="admin-action admin-action--restore" type="submit">Restore company' in (
        restore_response.text
    )


@pytest.mark.unit
@pytest.mark.integration
def test_preview_contact_detail_and_edit_routes_render_archive_and_restore_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "42")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    detail_archive = client.get(
        f"/admin/contacts/{PREVIEW_CRM_CONTACT_ACTIVE_ID}",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    detail_restore = client.get(
        f"/admin/contacts/{PREVIEW_CRM_CONTACT_ARCHIVED_ID}",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    edit_archive = client.get(
        f"/admin/contacts/{PREVIEW_CRM_CONTACT_ACTIVE_ID}/edit",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    edit_restore = client.get(
        f"/admin/contacts/{PREVIEW_CRM_CONTACT_ARCHIVED_ID}/edit",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    for response in (detail_archive, detail_restore, edit_archive, edit_restore):
        assert response.status_code == 200
    assert 'class="admin-action admin-action--destructive" type="submit">Archive contact' in (
        detail_archive.text
    )
    assert 'class="admin-action admin-action--restore" type="submit">Restore contact' in (
        detail_restore.text
    )
    assert 'class="admin-action admin-action--destructive" type="submit">Archive contact' in (
        edit_archive.text
    )
    assert 'class="admin-action admin-action--restore" type="submit">Restore contact' in (
        edit_restore.text
    )
