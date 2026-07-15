"""Archive/restore admin action button styling (#233)."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app.admin_auth import SESSION_COOKIE_NAME
from app.admin_contacts import render_contact_form_page
from app.admin_layout import render_archive_restore_form
from app.admin_preview import (
    PREVIEW_COMPANY_ARCHIVE_ID,
    PREVIEW_COMPANY_RESTORE_ID,
    PREVIEW_CONTACT_ARCHIVE_ID,
    PREVIEW_CONTACT_RESTORE_EDIT_ID,
    preview_company_detail,
    preview_contact_detail,
    preview_contact_edit,
)
from app.admin_research_pages import (
    render_admin_company_research_page,
    render_admin_contact_research_page,
)
from app.main import app

ADMIN_CSS = Path(__file__).resolve().parents[1] / "site/assets/admin.css"
COMPANY_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
CONTACT_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


@pytest.mark.unit
def test_render_archive_restore_form_uses_semantic_action_classes() -> None:
    archive_html = render_archive_restore_form(
        resource="company",
        record_id=COMPANY_ID,
        archived_at=None,
        csrf_token="csrf-token",
    )
    assert 'class="admin-action admin-action--destructive"' in archive_html
    assert "Archive company" in archive_html
    assert f'/admin/companies/{COMPANY_ID}/archive' in archive_html

    restore_html = render_archive_restore_form(
        resource="contact",
        record_id=CONTACT_ID,
        archived_at="2026-01-01",
        csrf_token="csrf-token",
    )
    assert 'class="admin-action admin-action--restore"' in restore_html
    assert "Restore contact" in restore_html
    assert f'/admin/contacts/{CONTACT_ID}/restore' in restore_html


@pytest.mark.unit
def test_company_research_page_archive_button_markup() -> None:
    html = render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme"},
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert 'class="admin-action admin-action--destructive"' in html
    assert "Archive company" in html
    assert 'class="admin-exit" type="submit">Archive company' not in html


@pytest.mark.unit
def test_company_research_page_restore_button_markup() -> None:
    html = render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme", "archived_at": "2026-01-01"},
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert 'class="admin-action admin-action--restore"' in html
    assert "Restore company" in html


@pytest.mark.unit
def test_contact_research_and_edit_archive_restore_markup() -> None:
    detail_html = render_admin_contact_research_page(
        contact={"id": CONTACT_ID, "full_name": "Pat", "buying_roles": []},
        company=None,
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert 'class="admin-action admin-action--destructive"' in detail_html
    assert "Archive contact" in detail_html

    edit_html = render_contact_form_page(
        admin_username="operator",
        csrf_token="csrf",
        companies=[],
        contact={"id": CONTACT_ID, "full_name": "Pat", "archived_at": "2026-01-01"},
    )
    assert 'class="admin-action admin-action--restore"' in edit_html
    assert "Restore contact" in edit_html


@pytest.mark.unit
def test_admin_action_css_resets_native_button_appearance() -> None:
    css = ADMIN_CSS.read_text(encoding="utf-8")
    action_block = css.split(".admin-action {", 1)[1].split("}", 1)[0]
    assert "font-family: inherit" in action_block
    assert "cursor: pointer" in action_block
    assert "border-radius:" in action_block
    assert "padding:" in action_block
    assert "background:" in action_block
    assert "border:" in action_block
    assert "color:" in action_block
    assert ".admin-action:focus-visible" in css
    assert ".admin-action:disabled" in css
    assert ".admin-action--destructive" in css
    assert ".admin-action--restore" in css
    destructive_block = css.split(".admin-action--destructive {", 1)[1].split("}", 1)[0]
    restore_block = css.split(".admin-action--restore {", 1)[1].split("}", 1)[0]
    assert destructive_block != restore_block
    assert "#ffb4b4" in destructive_block or "#e05a5a" in destructive_block
    assert "var(--accent)" in restore_block or "var(--ink)" in restore_block


@pytest.mark.unit
def test_preview_company_and_contact_archive_states_seed_stable() -> None:
    import random
    from datetime import datetime, timezone

    fixed_now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    archive_company, contacts_a, records_a = preview_company_detail(
        PREVIEW_COMPANY_ARCHIVE_ID,
        rng=random.Random(42),
        now=fixed_now,
    )
    archive_company_b, contacts_b, records_b = preview_company_detail(
        PREVIEW_COMPANY_ARCHIVE_ID,
        rng=random.Random(42),
        now=fixed_now,
    )
    assert archive_company == archive_company_b
    assert contacts_a == contacts_b
    assert records_a == records_b
    assert archive_company["archived_at"] is None

    restore_company, _, _ = preview_company_detail(
        PREVIEW_COMPANY_RESTORE_ID,
        rng=random.Random(42),
        now=fixed_now,
    )
    assert restore_company["archived_at"] is not None

    contact, company, records = preview_contact_detail(
        PREVIEW_CONTACT_ARCHIVE_ID,
        rng=random.Random(42),
        now=fixed_now,
    )
    assert contact["archived_at"] is None
    assert company is not None
    assert records

    archived_contact, companies = preview_contact_edit(
        PREVIEW_CONTACT_RESTORE_EDIT_ID,
        rng=random.Random(42),
    )
    assert archived_contact["archived_at"] is not None
    assert companies


@pytest.fixture
def preview_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
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
    return TestClient(app, follow_redirects=False)


@pytest.mark.unit
@pytest.mark.integration
def test_preview_routes_render_archive_and_restore_buttons(
    preview_client: TestClient,
) -> None:
    import random

    archive_company = preview_company_detail(
        PREVIEW_COMPANY_ARCHIVE_ID,
        rng=random.Random(42),
    )[0]
    company_archive = preview_client.get(
        f"/admin/companies/{PREVIEW_COMPANY_ARCHIVE_ID}",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert company_archive.status_code == 200
    assert archive_company["name"] in company_archive.text
    assert 'class="admin-action admin-action--destructive"' in company_archive.text
    assert "Archive company" in company_archive.text

    company_restore = preview_client.get(
        f"/admin/companies/{PREVIEW_COMPANY_RESTORE_ID}",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert company_restore.status_code == 200
    assert 'class="admin-action admin-action--restore"' in company_restore.text
    assert "Restore company" in company_restore.text

    contact_detail = preview_contact_detail(
        PREVIEW_CONTACT_ARCHIVE_ID,
        rng=random.Random(42),
    )[0]
    contact_archive = preview_client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_ARCHIVE_ID}",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert contact_archive.status_code == 200
    assert contact_detail["full_name"] in contact_archive.text
    assert 'class="admin-action admin-action--destructive"' in contact_archive.text
    assert "Archive contact" in contact_archive.text

    archived_contact = preview_contact_edit(
        PREVIEW_CONTACT_RESTORE_EDIT_ID,
        rng=random.Random(42),
    )[0]
    contact_restore = preview_client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_RESTORE_EDIT_ID}/edit",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert contact_restore.status_code == 200
    assert archived_contact["full_name"] in contact_restore.text
    assert 'class="admin-action admin-action--restore"' in contact_restore.text
    assert "Restore contact" in contact_restore.text
