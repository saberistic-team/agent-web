"""Archive/Restore admin action button styling (#233)."""

from __future__ import annotations

import random
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.admin_auth import SESSION_COOKIE_NAME
from app.admin_contacts import render_contact_form_page
from app.admin_layout import archive_action_button
from app.admin_preview import (
    PREVIEW_COMPANY_DETAIL_ARCHIVE_ID,
    PREVIEW_COMPANY_DETAIL_RESTORE_ID,
    PREVIEW_CONTACT_DETAIL_ARCHIVE_ID,
    PREVIEW_CONTACT_DETAIL_RESTORE_ID,
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
COMPANY_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
CONTACT_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
client = TestClient(app, follow_redirects=False)


@pytest.mark.unit
def test_archive_action_button_renders_destructive_and_secondary_classes() -> None:
    archive = archive_action_button(label="Archive company", archived=False)
    restore = archive_action_button(label="Restore company", archived=True)
    assert 'class="admin-action admin-action--destructive"' in archive
    assert "Archive company" in archive
    assert 'class="admin-action admin-action--secondary"' in restore
    assert "Restore company" in restore
    assert "admin-exit" not in archive
    assert "admin-exit" not in restore


@pytest.mark.unit
def test_company_research_page_archive_and_restore_use_action_classes() -> None:
    archive_html = render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme"},
        contacts=[],
        records=[],
        csrf_token="csrf",
    )
    assert 'class="admin-action admin-action--destructive"' in archive_html
    assert "Archive company" in archive_html
    assert 'action="/admin/companies/' in archive_html
    assert "/archive" in archive_html

    restore_html = render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme", "archived_at": "2026-01-01"},
        contacts=[],
        records=[],
        csrf_token="csrf",
    )
    assert 'class="admin-action admin-action--secondary"' in restore_html
    assert "Restore company" in restore_html
    assert "/restore" in restore_html


@pytest.mark.unit
def test_contact_research_and_edit_pages_use_action_classes() -> None:
    detail_html = render_admin_contact_research_page(
        contact={"id": CONTACT_ID, "full_name": "Pat"},
        company=None,
        records=[],
        csrf_token="csrf",
    )
    assert 'class="admin-action admin-action--destructive"' in detail_html
    assert "Archive contact" in detail_html

    edit_html = render_contact_form_page(
        admin_username="operator",
        csrf_token="csrf",
        companies=[],
        contact={"id": CONTACT_ID, "full_name": "Pat", "archived_at": "2026-01-01"},
    )
    assert 'class="admin-action admin-action--secondary"' in edit_html
    assert "Restore contact" in edit_html


@pytest.mark.unit
def test_admin_css_action_buttons_reset_native_appearance() -> None:
    css = ADMIN_CSS.read_text(encoding="utf-8")
    base_block = css.split(".admin-action {", 1)[1].split("}", 1)[0]
    assert "background:" in base_block
    assert "border:" in base_block
    assert "padding:" in base_block
    assert "font-family:" in base_block
    assert "cursor: pointer" in base_block
    assert "border-radius:" in base_block
    assert ":focus-visible" in css.split(".admin-action", 1)[1]
    assert ":disabled" in css
    assert ".admin-action--destructive" in css
    assert ".admin-action--secondary" in css
    destructive_block = css.split(".admin-action--destructive {", 1)[1].split("}", 1)[0]
    assert "background:" in destructive_block
    assert "border-color:" in destructive_block
    assert "color:" in destructive_block
    assert "#ffb4b4" in destructive_block


@pytest.mark.unit
def test_preview_company_and_contact_archive_states_stable_with_seed() -> None:
    from datetime import datetime, timezone

    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    company_archive = preview_company_research_detail(
        PREVIEW_COMPANY_DETAIL_ARCHIVE_ID, rng=random.Random(42), now=now
    )
    company_restore = preview_company_research_detail(
        PREVIEW_COMPANY_DETAIL_RESTORE_ID, rng=random.Random(42), now=now
    )
    contact_archive = preview_contact_research_detail(
        PREVIEW_CONTACT_DETAIL_ARCHIVE_ID, rng=random.Random(42), now=now
    )
    contact_restore = preview_contact_research_detail(
        PREVIEW_CONTACT_DETAIL_RESTORE_ID, rng=random.Random(42), now=now
    )
    assert company_archive is not None
    assert company_restore is not None
    assert contact_archive is not None
    assert contact_restore is not None
    assert company_archive[0].get("archived_at") is None
    assert company_restore[0].get("archived_at") is not None
    assert contact_archive[0].get("archived_at") is None
    assert contact_restore[0].get("archived_at") is not None

    assert preview_company_research_detail(
        PREVIEW_COMPANY_DETAIL_ARCHIVE_ID, rng=random.Random(42), now=now
    ) == company_archive
    assert preview_contact_edit_detail(
        PREVIEW_CONTACT_DETAIL_RESTORE_ID, rng=random.Random(42), now=now
    ) == preview_contact_edit_detail(
        PREVIEW_CONTACT_DETAIL_RESTORE_ID, rng=random.Random(42), now=now
    )


@pytest.mark.unit
@pytest.mark.integration
def test_preview_routes_render_archive_and_restore_action_buttons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "42")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cookie = {SESSION_COOKIE_NAME: "preview-screenshot-session"}
    cases = (
        (f"/admin/companies/{PREVIEW_COMPANY_DETAIL_ARCHIVE_ID}", "Archive company", "destructive"),
        (f"/admin/companies/{PREVIEW_COMPANY_DETAIL_RESTORE_ID}", "Restore company", "secondary"),
        (f"/admin/contacts/{PREVIEW_CONTACT_DETAIL_ARCHIVE_ID}", "Archive contact", "destructive"),
        (
            f"/admin/contacts/{PREVIEW_CONTACT_DETAIL_ARCHIVE_ID}/edit",
            "Archive contact",
            "destructive",
        ),
        (f"/admin/contacts/{PREVIEW_CONTACT_DETAIL_RESTORE_ID}", "Restore contact", "secondary"),
        (
            f"/admin/contacts/{PREVIEW_CONTACT_DETAIL_RESTORE_ID}/edit",
            "Restore contact",
            "secondary",
        ),
    )
    for path, label, modifier in cases:
        response = client.get(path, cookies=cookie)
        assert response.status_code == 200, path
        body = response.text
        assert label in body, path
        assert f'admin-action--{modifier}' in body, path
        assert 'class="admin-exit" type="submit"' not in body, path
