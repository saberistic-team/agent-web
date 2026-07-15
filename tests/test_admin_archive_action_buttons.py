"""Archive/restore admin action button styling (#233)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app import admin_contacts, admin_research_pages
from app.admin_layout import archive_restore_button_class
from app.admin_preview import (
    PREVIEW_COMPANY_ARCHIVE_DETAIL_ID,
    PREVIEW_COMPANY_RESTORE_DETAIL_ID,
    PREVIEW_CONTACT_ARCHIVE_DETAIL_ID,
    PREVIEW_CONTACT_RESTORE_DETAIL_ID,
    build_preview_company_detail,
    build_preview_contact_detail,
)
from app.main import app

ADMIN_CSS = Path(__file__).resolve().parents[1] / "site/assets/admin.css"
COMPANY_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
CONTACT_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
SESSION_COOKIE_NAME = "admin_session"


@pytest.mark.unit
def test_archive_restore_button_class_semantics() -> None:
    assert archive_restore_button_class(archived_at=None) == (
        "admin-action-btn admin-action-btn--destructive"
    )
    assert archive_restore_button_class(archived_at="2026-01-01") == (
        "admin-action-btn admin-action-btn--secondary"
    )


@pytest.mark.unit
def test_company_detail_renders_destructive_archive_button() -> None:
    html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme"},
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert 'class="admin-action-btn admin-action-btn--destructive"' in html
    assert "Archive company" in html
    archive_button = html.split("Archive company")[0].rsplit("<button", 1)[-1]
    assert "admin-exit" not in archive_button


@pytest.mark.unit
def test_company_detail_renders_secondary_restore_button() -> None:
    html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme", "archived_at": "2026-01-01"},
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert 'class="admin-action-btn admin-action-btn--secondary"' in html
    assert "Restore company" in html
    archive_region = html.split("Restore company")[0].rsplit("<button", 1)[-1]
    assert "admin-exit" not in archive_region


@pytest.mark.unit
def test_contact_detail_and_edit_render_themed_archive_buttons() -> None:
    active_detail = admin_research_pages.render_admin_contact_research_page(
        contact={"id": CONTACT_ID, "full_name": "Pat", "buying_roles": []},
        company=None,
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert 'class="admin-action-btn admin-action-btn--destructive"' in active_detail
    assert "Archive contact" in active_detail

    archived_detail = admin_research_pages.render_admin_contact_research_page(
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
    assert 'class="admin-action-btn admin-action-btn--secondary"' in archived_detail
    assert "Restore contact" in archived_detail

    edit_html = admin_contacts.render_contact_form_page(
        admin_username="operator",
        csrf_token="csrf",
        companies=[],
        contact={"id": CONTACT_ID, "full_name": "Pat"},
    )
    assert 'class="admin-action-btn admin-action-btn--destructive"' in edit_html
    assert "Archive contact" in edit_html
    assert "admin-exit" not in edit_html.split("Archive contact")[0].rsplit("<button", 1)[-1]


@pytest.mark.unit
def test_admin_action_btn_css_resets_native_button_appearance() -> None:
    css = ADMIN_CSS.read_text(encoding="utf-8")
    base_block = css.split(".admin-action-btn {", 1)[1].split("}", 1)[0]
    destructive_block = css.split(".admin-action-btn--destructive {", 1)[1].split("}", 1)[0]
    secondary_block = css.split(".admin-action-btn--secondary {", 1)[1].split("}", 1)[0]

    for block in (destructive_block, secondary_block):
        assert "background:" in block
        assert "border:" in block or "border-color:" in block
    assert "padding:" in base_block
    assert "cursor: pointer" in base_block
    assert "border-radius:" in base_block
    assert "font-family: inherit" in base_block
    assert "outline:" in css.split(".admin-action-btn--destructive:focus-visible", 1)[1]
    assert "outline:" in css.split(".admin-action-btn--secondary:focus-visible", 1)[1]
    assert ".admin-action-btn:disabled" in css
    assert "opacity: 0.45" in css
    assert "cursor: not-allowed" in css.split(".admin-action-btn:disabled", 1)[1]

    # Destructive archive styling must not read as disabled muted gray.
    assert "var(--muted)" not in destructive_block


@pytest.mark.unit
def test_preview_company_and_contact_detail_archive_states(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "42")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    client = TestClient(app, follow_redirects=False)

    archive_company = client.get(
        f"/admin/companies/{PREVIEW_COMPANY_ARCHIVE_DETAIL_ID}",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert archive_company.status_code == 200
    assert 'class="admin-action-btn admin-action-btn--destructive"' in archive_company.text
    assert "Archive company" in archive_company.text
    assert "Northwind Labs" in archive_company.text

    restore_company = client.get(
        f"/admin/companies/{PREVIEW_COMPANY_RESTORE_DETAIL_ID}",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert restore_company.status_code == 200
    assert 'class="admin-action-btn admin-action-btn--secondary"' in restore_company.text
    assert "Restore company" in restore_company.text
    assert "Helios Rail" in restore_company.text

    archive_contact = client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_ARCHIVE_DETAIL_ID}",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert archive_contact.status_code == 200
    assert 'class="admin-action-btn admin-action-btn--destructive"' in archive_contact.text
    assert "Archive contact" in archive_contact.text

    restore_contact = client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_RESTORE_DETAIL_ID}/edit",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert restore_contact.status_code == 200
    assert 'class="admin-action-btn admin-action-btn--secondary"' in restore_contact.text
    assert "Restore contact" in restore_contact.text


@pytest.mark.unit
def test_preview_detail_builders_seed_stable() -> None:
    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    company_a = build_preview_company_detail(PREVIEW_COMPANY_ARCHIVE_DETAIL_ID, now=now)
    company_b = build_preview_company_detail(PREVIEW_COMPANY_ARCHIVE_DETAIL_ID, now=now)
    assert company_a == company_b
    assert company_a is not None
    assert company_a["company"]["archived_at"] is None

    contact_a = build_preview_contact_detail(PREVIEW_CONTACT_RESTORE_DETAIL_ID, now=now)
    contact_b = build_preview_contact_detail(PREVIEW_CONTACT_RESTORE_DETAIL_ID, now=now)
    assert contact_a == contact_b
    assert contact_a is not None
    assert contact_a["contact"]["archived_at"] is not None
