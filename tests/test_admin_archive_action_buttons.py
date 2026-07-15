"""Tests for themed archive/restore admin action buttons (#233)."""

from __future__ import annotations

import random
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app import admin_contacts, admin_research_pages
from app.admin_layout import archive_action_button_class
from app.admin_preview import (
    PREVIEW_COMPANY_ARCHIVE_ID,
    PREVIEW_COMPANY_RESTORE_ID,
    PREVIEW_CONTACT_ARCHIVE_ID,
    PREVIEW_CONTACT_RESTORE_ID,
    build_preview_company_research,
    build_preview_contact_edit,
    build_preview_contact_research,
)
from app.main import app

ADMIN_CSS = Path(__file__).resolve().parents[1] / "site/assets/admin.css"

COMPANY_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
CONTACT_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


@pytest.mark.unit
def test_archive_action_button_class_semantic_variants() -> None:
    assert archive_action_button_class(archived=False) == (
        "admin-action admin-action--destructive"
    )
    assert archive_action_button_class(archived=True) == "admin-action admin-action--restore"


@pytest.mark.unit
def test_company_research_page_archive_button_uses_destructive_action_class() -> None:
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
def test_company_research_page_restore_button_uses_restore_action_class() -> None:
    html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme", "archived_at": "2026-07-01"},
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert 'class="admin-action admin-action--restore" type="submit">Restore company' in html


@pytest.mark.unit
def test_contact_research_page_archive_and_restore_action_classes() -> None:
    archive_html = admin_research_pages.render_admin_contact_research_page(
        contact={"id": CONTACT_ID, "full_name": "Pat", "buying_roles": []},
        company=None,
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert (
        'class="admin-action admin-action--destructive" type="submit">Archive contact'
        in archive_html
    )

    restore_html = admin_research_pages.render_admin_contact_research_page(
        contact={
            "id": CONTACT_ID,
            "full_name": "Pat",
            "buying_roles": [],
            "archived_at": "2026-07-01",
        },
        company=None,
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert (
        'class="admin-action admin-action--restore" type="submit">Restore contact'
        in restore_html
    )


@pytest.mark.unit
def test_contact_edit_page_archive_and_restore_action_classes() -> None:
    archive_html = admin_contacts.render_contact_form_page(
        admin_username="operator",
        csrf_token="csrf",
        companies=[],
        contact={"id": CONTACT_ID, "full_name": "Pat"},
    )
    assert (
        'class="admin-action admin-action--destructive" type="submit">Archive contact'
        in archive_html
    )

    restore_html = admin_contacts.render_contact_form_page(
        admin_username="operator",
        csrf_token="csrf",
        companies=[],
        contact={"id": CONTACT_ID, "full_name": "Pat", "archived_at": "2026-07-01"},
    )
    assert (
        'class="admin-action admin-action--restore" type="submit">Restore contact'
        in restore_html
    )


@pytest.mark.unit
def test_admin_css_action_buttons_reset_native_appearance() -> None:
    css = ADMIN_CSS.read_text(encoding="utf-8")
    action_block = css.split(".admin-action {", 1)[1].split("}", 1)[0]
    assert "background:" in action_block
    assert "border:" in action_block
    assert "padding:" in action_block
    assert "font-family:" in action_block
    assert "color:" in action_block
    assert "cursor:" in action_block
    assert "border-radius:" in action_block
    assert "background: none" not in action_block
    assert "background: white" not in action_block.lower()
    assert ".admin-action:focus-visible" in css
    assert ".admin-action:disabled" in css
    assert ".admin-action--destructive" in css
    assert ".admin-action--restore" in css
    destructive_block = css.split(".admin-action--destructive {", 1)[1].split("}", 1)[0]
    restore_block = css.split(".admin-action--restore {", 1)[1].split("}", 1)[0]
    assert destructive_block != restore_block


@pytest.mark.unit
def test_preview_company_and_contact_detail_archive_restore_buttons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    company_archive = client.get(f"/admin/companies/{PREVIEW_COMPANY_ARCHIVE_ID}")
    company_restore = client.get(f"/admin/companies/{PREVIEW_COMPANY_RESTORE_ID}")
    contact_archive = client.get(f"/admin/contacts/{PREVIEW_CONTACT_ARCHIVE_ID}")
    contact_restore = client.get(f"/admin/contacts/{PREVIEW_CONTACT_RESTORE_ID}")
    contact_edit_archive = client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_ARCHIVE_ID}/edit"
    )
    contact_edit_restore = client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_RESTORE_ID}/edit"
    )

    for response in (
        company_archive,
        company_restore,
        contact_archive,
        contact_restore,
        contact_edit_archive,
        contact_edit_restore,
    ):
        assert response.status_code == 200
        assert "admin-action" in response.text
        assert 'class="admin-exit" type="submit">Archive' not in response.text
        assert 'class="admin-exit" type="submit">Restore' not in response.text

    assert (
        'class="admin-action admin-action--destructive" type="submit">Archive company'
        in company_archive.text
    )
    assert (
        'class="admin-action admin-action--restore" type="submit">Restore company'
        in company_restore.text
    )
    assert (
        'class="admin-action admin-action--destructive" type="submit">Archive contact'
        in contact_archive.text
    )
    assert (
        'class="admin-action admin-action--restore" type="submit">Restore contact'
        in contact_restore.text
    )


@pytest.mark.unit
def test_preview_archive_fixtures_seed_stable() -> None:
    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    company_a = build_preview_company_research(
        PREVIEW_COMPANY_ARCHIVE_ID, rng=random.Random(42), now=now
    )
    company_b = build_preview_company_research(
        PREVIEW_COMPANY_ARCHIVE_ID, rng=random.Random(42), now=now
    )
    assert company_a == company_b
    assert company_a is not None
    assert company_a[0].get("archived_at") is None

    contact_a = build_preview_contact_research(
        PREVIEW_CONTACT_RESTORE_ID, rng=random.Random(42), now=now
    )
    contact_b = build_preview_contact_research(
        PREVIEW_CONTACT_RESTORE_ID, rng=random.Random(42), now=now
    )
    assert contact_a == contact_b
    assert contact_a is not None
    assert contact_a[0].get("archived_at") is not None

    edit_a = build_preview_contact_edit(
        PREVIEW_CONTACT_ARCHIVE_ID, rng=random.Random(42), now=now
    )
    edit_b = build_preview_contact_edit(
        PREVIEW_CONTACT_ARCHIVE_ID, rng=random.Random(42), now=now
    )
    assert edit_a == edit_b
