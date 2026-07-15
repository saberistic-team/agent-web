"""Archive/Restore admin action button styling and markup (#233)."""

from __future__ import annotations

import random
from pathlib import Path
from uuid import UUID

import pytest

from app.admin_contacts import render_contact_form_page
from app.admin_auth import SESSION_COOKIE_NAME
from app.admin_layout import archive_restore_button_class, render_archive_restore_button
from app.admin_preview import (
    PREVIEW_COMPANY_ARCHIVE_DETAIL_ID,
    PREVIEW_COMPANY_RESTORE_DETAIL_ID,
    PREVIEW_CONTACT_ARCHIVE_DETAIL_ID,
    PREVIEW_CONTACT_RESTORE_DETAIL_ID,
    preview_company_research_detail,
    preview_contact_edit_detail,
    preview_contact_research_detail,
)
from app.admin_research_pages import (
    render_admin_company_research_page,
    render_admin_contact_research_page,
)

ADMIN_CSS = Path(__file__).resolve().parents[1] / "site/assets/admin.css"

COMPANY_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
CONTACT_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


@pytest.mark.unit
def test_archive_restore_button_classes_are_semantic() -> None:
    assert archive_restore_button_class(archived=False) == (
        "admin-action-btn admin-action-btn--destructive"
    )
    assert archive_restore_button_class(archived=True) == (
        "admin-action-btn admin-action-btn--restore"
    )


@pytest.mark.unit
def test_render_archive_restore_button_escapes_label() -> None:
    button = render_archive_restore_button(label='Archive "Acme"', archived=False)
    assert 'class="admin-action-btn admin-action-btn--destructive"' in button
    assert "Archive &quot;Acme&quot;" in button
    assert "admin-exit" not in button


@pytest.mark.unit
def test_company_research_page_renders_themed_archive_button() -> None:
    html = render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme"},
        contacts=[],
        records=[],
        csrf_token="csrf",
    )
    assert 'class="admin-action-btn admin-action-btn--destructive"' in html
    assert "Archive company" in html
    assert 'action="/admin/companies/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/archive"' in html
    assert 'class="admin-exit" type="submit"' not in html


@pytest.mark.unit
def test_company_research_page_renders_themed_restore_button() -> None:
    html = render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme", "archived_at": "2026-01-01"},
        contacts=[],
        records=[],
        csrf_token="csrf",
    )
    assert 'class="admin-action-btn admin-action-btn--restore"' in html
    assert "Restore company" in html
    assert "/restore" in html


@pytest.mark.unit
def test_contact_research_and_edit_pages_render_themed_buttons() -> None:
    contact = {"id": CONTACT_ID, "full_name": "Pat Example"}
    detail_html = render_admin_contact_research_page(
        contact=contact,
        company=None,
        records=[],
        csrf_token="csrf",
    )
    assert 'class="admin-action-btn admin-action-btn--destructive"' in detail_html
    assert "Archive contact" in detail_html

    edit_html = render_contact_form_page(
        admin_username="operator",
        csrf_token="csrf",
        companies=[],
        contact={**contact, "archived_at": "2026-01-01"},
    )
    assert 'class="admin-action-btn admin-action-btn--restore"' in edit_html
    assert "Restore contact" in edit_html
    assert 'class="admin-exit" type="submit">Restore contact' not in edit_html


@pytest.mark.unit
def test_admin_css_archive_restore_buttons_reset_native_appearance() -> None:
    css = ADMIN_CSS.read_text(encoding="utf-8")
    base_block = css.split(".admin-action-btn {", 1)[1].split("}", 1)[0]
    assert "background:" in base_block
    assert "border:" in base_block
    assert "padding:" in base_block
    assert "font-family: inherit" in base_block
    assert "cursor: pointer" in base_block
    assert "border-radius:" in base_block

    destructive_block = css.split(".admin-action-btn--destructive {", 1)[1].split("}", 1)[0]
    restore_block = css.split(".admin-action-btn--restore {", 1)[1].split("}", 1)[0]
    assert "background:" in destructive_block
    assert "background:" in restore_block
    assert destructive_block != restore_block

    assert ".admin-action-btn:focus-visible" in css
    assert ".admin-action-btn:active" in css
    assert ".admin-action-btn:disabled" in css
    assert ".admin-action-btn--destructive:disabled" in css
    assert ".admin-action-btn--restore:disabled" in css


@pytest.mark.unit
def test_preview_company_and_contact_detail_fixtures_stable_with_seed() -> None:
    company_archive = preview_company_research_detail(
        company_id=PREVIEW_COMPANY_ARCHIVE_DETAIL_ID,
        archived=False,
        rng=random.Random(23),
    )
    company_restore = preview_company_research_detail(
        company_id=PREVIEW_COMPANY_RESTORE_DETAIL_ID,
        archived=True,
        rng=random.Random(23),
    )
    assert company_archive["company"]["archived_at"] is None
    assert company_restore["company"]["archived_at"] is not None
    assert company_archive["records"]

    contact_archive = preview_contact_research_detail(
        contact_id=PREVIEW_CONTACT_ARCHIVE_DETAIL_ID,
        archived=False,
        rng=random.Random(23),
    )
    contact_restore = preview_contact_edit_detail(
        contact_id=PREVIEW_CONTACT_RESTORE_DETAIL_ID,
        archived=True,
        rng=random.Random(23),
    )
    assert contact_archive["contact"]["archived_at"] is None
    assert contact_restore["contact"]["archived_at"] is not None


@pytest.mark.unit
@pytest.mark.integration
def test_preview_routes_render_archive_and_restore_buttons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", "operator")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", "unused")
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "secret-secret-secret-secret")
    monkeypatch.setenv("BASE_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")

    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    cookie = {SESSION_COOKIE_NAME: "preview-screenshot-session"}

    company_archive = client.get(
        f"/admin/companies/{PREVIEW_COMPANY_ARCHIVE_DETAIL_ID}",
        cookies=cookie,
    )
    assert company_archive.status_code == 200
    assert 'class="admin-action-btn admin-action-btn--destructive"' in company_archive.text
    assert "Archive company" in company_archive.text

    company_restore = client.get(
        f"/admin/companies/{PREVIEW_COMPANY_RESTORE_DETAIL_ID}",
        cookies=cookie,
    )
    assert company_restore.status_code == 200
    assert 'class="admin-action-btn admin-action-btn--restore"' in company_restore.text

    contact_archive = client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_ARCHIVE_DETAIL_ID}",
        cookies=cookie,
    )
    assert contact_archive.status_code == 200
    assert 'class="admin-action-btn admin-action-btn--destructive"' in contact_archive.text

    contact_restore_edit = client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_RESTORE_DETAIL_ID}/edit",
        cookies=cookie,
    )
    assert contact_restore_edit.status_code == 200
    assert 'class="admin-action-btn admin-action-btn--restore"' in contact_restore_edit.text
