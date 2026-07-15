"""Archive/restore admin action button styling and markup (#233)."""

from __future__ import annotations

import random
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app import admin_contacts, admin_research_pages
from app.admin_auth import SESSION_COOKIE_NAME
from app.admin_layout import render_admin_archive_form
from app.admin_preview import (
    PREVIEW_COMPANY_ARCHIVE_ID,
    PREVIEW_COMPANY_RESTORE_ID,
    PREVIEW_CONTACT_ARCHIVE_ID,
    PREVIEW_CONTACT_RESTORE_ID,
    build_preview_company_detail,
    build_preview_contact_detail,
)
from app.main import app

ADMIN_CSS = Path(__file__).resolve().parents[1] / "site/assets/admin.css"
COMPANY_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
CONTACT_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

client = TestClient(app, follow_redirects=False)


def _admin_css() -> str:
    return ADMIN_CSS.read_text(encoding="utf-8")


def _rule_block(css: str, selector_fragment: str) -> str:
    start = css.index(selector_fragment)
    brace_start = css.index("{", start)
    depth = 0
    for index, char in enumerate(css[brace_start:], start=brace_start):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return css[start : index + 1]
    raise AssertionError(f"Unclosed rule for {selector_fragment!r}")


def _archive_button_markup(html: str, label: str) -> str:
    match = re.search(
        rf'<button class="admin-action-btn [^"]+" type="submit">{re.escape(label)}</button>',
        html,
    )
    assert match is not None, f"Expected themed archive button for {label!r}"
    return match.group(0)


@pytest.mark.unit
def test_admin_action_btn_resets_native_button_appearance() -> None:
    css = _admin_css()
    base = _rule_block(css, ".admin-action-btn {")
    assert "font-family: inherit" in base
    assert "padding: 0.5rem 0.85rem" in base
    assert "border-radius: 2px" in base
    assert "cursor: pointer" in base
    assert "background:" in base
    assert "border: 1px solid" in base
    assert "color:" in base

    destructive = _rule_block(css, ".admin-action-btn--destructive {")
    assert "background:" in destructive
    assert "#e05a5a" in destructive

    secondary = _rule_block(css, ".admin-action-btn--secondary {")
    assert "background: transparent" in secondary

    assert ".admin-action-btn:focus-visible" in css
    assert ".admin-action-btn:hover:not(:disabled)" in css
    assert ".admin-action-btn:active:not(:disabled)" in css
    assert ".admin-action-btn:disabled" in css


@pytest.mark.unit
def test_admin_exit_does_not_reset_submit_button_appearance() -> None:
    css = _admin_css()
    exit_block = _rule_block(css, ".admin-exit {")
    assert "background:" not in exit_block
    assert "border:" not in exit_block
    assert "padding:" not in exit_block


@pytest.mark.unit
def test_render_admin_archive_form_uses_semantic_classes() -> None:
    archive = render_admin_archive_form(
        action_url="/admin/companies/1/archive",
        label="Archive company",
        is_archived=False,
        csrf_token="csrf",
    )
    assert 'class="admin-action-btn admin-action-btn--destructive"' in archive
    assert "admin-exit" not in archive

    restore = render_admin_archive_form(
        action_url="/admin/companies/1/restore",
        label="Restore company",
        is_archived=True,
        csrf_token="csrf",
    )
    assert 'class="admin-action-btn admin-action-btn--secondary"' in restore


@pytest.mark.unit
def test_company_research_page_archive_and_restore_markup() -> None:
    archive_html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme"},
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    button = _archive_button_markup(archive_html, "Archive company")
    assert "admin-exit" not in button

    restore_html = admin_research_pages.render_admin_company_research_page(
        company={
            "id": COMPANY_ID,
            "name": "Acme",
            "archived_at": "2026-07-01T00:00:00+00:00",
        },
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    _archive_button_markup(restore_html, "Restore company")
    assert 'admin-action-btn--secondary' in restore_html


@pytest.mark.unit
def test_contact_research_and_edit_pages_archive_and_restore_markup() -> None:
    contact = {
        "id": CONTACT_ID,
        "full_name": "Ada Lovelace",
        "company_id": COMPANY_ID,
        "buying_roles": [],
    }
    detail_archive = admin_research_pages.render_admin_contact_research_page(
        contact=contact,
        company={"id": COMPANY_ID, "name": "Acme"},
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    _archive_button_markup(detail_archive, "Archive contact")

    detail_restore = admin_research_pages.render_admin_contact_research_page(
        contact={**contact, "archived_at": "2026-07-01T00:00:00+00:00"},
        company={"id": COMPANY_ID, "name": "Acme"},
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    _archive_button_markup(detail_restore, "Restore contact")

    edit_archive = admin_contacts.render_contact_form_page(
        csrf_token="csrf",
        admin_username="operator",
        companies=[{"id": COMPANY_ID, "name": "Acme"}],
        contact=contact,
    )
    _archive_button_markup(edit_archive, "Archive contact")

    edit_restore = admin_contacts.render_contact_form_page(
        csrf_token="csrf",
        admin_username="operator",
        companies=[],
        contact={**contact, "archived_at": "2026-01-01"},
    )
    _archive_button_markup(edit_restore, "Restore contact")


@pytest.mark.unit
def test_preview_company_and_contact_detail_seed_stable() -> None:
    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    company_a, contacts_a, records_a = build_preview_company_detail(
        PREVIEW_COMPANY_ARCHIVE_ID, rng=random.Random(42), now=now
    )
    company_b, contacts_b, records_b = build_preview_company_detail(
        PREVIEW_COMPANY_ARCHIVE_ID, rng=random.Random(42), now=now
    )
    assert company_a == company_b
    assert contacts_a == contacts_b
    assert records_a == records_b
    assert company_a.get("archived_at") is None

    restore_company, _, _ = build_preview_company_detail(
        PREVIEW_COMPANY_RESTORE_ID, rng=random.Random(42), now=now
    )
    assert restore_company.get("archived_at") is not None

    contact_a, company_a, records_a = build_preview_contact_detail(
        PREVIEW_CONTACT_ARCHIVE_ID, rng=random.Random(42), now=now
    )
    contact_b, company_b, records_b = build_preview_contact_detail(
        PREVIEW_CONTACT_ARCHIVE_ID, rng=random.Random(42), now=now
    )
    assert contact_a == contact_b
    assert company_a == company_b
    assert records_a == records_b
    assert contact_a.get("archived_at") is None

    restore_contact, _, _ = build_preview_contact_detail(
        PREVIEW_CONTACT_RESTORE_ID, rng=random.Random(42), now=now
    )
    assert restore_contact.get("archived_at") is not None


@pytest.mark.unit
def test_preview_routes_render_themed_archive_buttons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "7")
    monkeypatch.setenv("ADMIN_USERNAME", "preview-admin")
    monkeypatch.setenv(
        "ADMIN_PASSWORD_HASH",
        PasswordHasher().hash("preview"),
    )
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "preview-session-secret-32chars-minimum")
    monkeypatch.setenv("BASE_URL", "http://127.0.0.1:8765")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    preview_client = TestClient(app, follow_redirects=False)
    cookie = {SESSION_COOKIE_NAME: "preview-screenshot-session"}

    company_archive = preview_client.get(
        f"/admin/companies/{PREVIEW_COMPANY_ARCHIVE_ID}",
        cookies=cookie,
    )
    assert company_archive.status_code == 200
    _archive_button_markup(company_archive.text, "Archive company")

    company_restore = preview_client.get(
        f"/admin/companies/{PREVIEW_COMPANY_RESTORE_ID}",
        cookies=cookie,
    )
    assert company_restore.status_code == 200
    _archive_button_markup(company_restore.text, "Restore company")

    contact_archive = preview_client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_ARCHIVE_ID}",
        cookies=cookie,
    )
    assert contact_archive.status_code == 200
    _archive_button_markup(contact_archive.text, "Archive contact")

    contact_restore = preview_client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_RESTORE_ID}",
        cookies=cookie,
    )
    assert contact_restore.status_code == 200
    _archive_button_markup(contact_restore.text, "Restore contact")

    contact_edit_archive = preview_client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_ARCHIVE_ID}/edit",
        cookies=cookie,
    )
    assert contact_edit_archive.status_code == 200
    _archive_button_markup(contact_edit_archive.text, "Archive contact")

    contact_edit_restore = preview_client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_RESTORE_ID}/edit",
        cookies=cookie,
    )
    assert contact_edit_restore.status_code == 200
    _archive_button_markup(contact_edit_restore.text, "Restore contact")
