"""Regression tests for themed archive/restore admin action buttons (#233)."""

from __future__ import annotations

import random
import re
from pathlib import Path
from uuid import UUID

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app import admin_contacts, admin_research_pages
from app.admin_auth import SESSION_COOKIE_NAME
from app.admin_layout import render_admin_archive_action_button
from app.admin_preview import (
    PREVIEW_COMPANY_ARCHIVE_ID,
    PREVIEW_COMPANY_RESTORE_ID,
    PREVIEW_CONTACT_ARCHIVE_ID,
    PREVIEW_CONTACT_RESTORE_ID,
    preview_company_detail,
    preview_contact_detail,
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


def _archive_button_markup(html: str, label: str) -> str | None:
    pattern = (
        rf'<button class="admin-action-btn admin-action-btn--(?:destructive|secondary)" '
        rf'type="submit">{re.escape(label)}</button>'
    )
    match = re.search(pattern, html)
    return match.group(0) if match else None


@pytest.mark.unit
def test_render_admin_archive_action_button_uses_semantic_modifiers() -> None:
    archive = render_admin_archive_action_button(label="Archive company", archived=False)
    restore = render_admin_archive_action_button(label="Restore company", archived=True)
    assert 'class="admin-action-btn admin-action-btn--destructive"' in archive
    assert "Archive company" in archive
    assert 'class="admin-action-btn admin-action-btn--secondary"' in restore
    assert "Restore company" in restore
    assert "admin-exit" not in archive
    assert "admin-exit" not in restore


@pytest.mark.unit
def test_company_research_page_archive_and_restore_markup() -> None:
    archive_html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme"},
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert _archive_button_markup(archive_html, "Archive company")
    assert 'action="/admin/companies/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/archive"' in archive_html
    assert "admin-exit" not in archive_html.split("Archive company")[1].split("</button>")[0]

    restore_html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme", "archived_at": "2026-01-01"},
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert _archive_button_markup(restore_html, "Restore company")
    assert 'admin-action-btn--secondary' in restore_html
    assert 'action="/admin/companies/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/restore"' in restore_html


@pytest.mark.unit
def test_contact_research_and_edit_pages_archive_and_restore_markup() -> None:
    contact = {
        "id": CONTACT_ID,
        "full_name": "Pat Example",
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
    assert _archive_button_markup(detail_archive, "Archive contact")

    detail_restore = admin_research_pages.render_admin_contact_research_page(
        contact={**contact, "archived_at": "2026-01-01"},
        company={"id": COMPANY_ID, "name": "Acme"},
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert _archive_button_markup(detail_restore, "Restore contact")
    assert 'admin-action-btn--secondary' in detail_restore

    edit_archive = admin_contacts.render_contact_form_page(
        admin_username="operator",
        csrf_token="csrf",
        companies=[{"id": COMPANY_ID, "name": "Acme"}],
        contact=contact,
    )
    assert _archive_button_markup(edit_archive, "Archive contact")

    edit_restore = admin_contacts.render_contact_form_page(
        admin_username="operator",
        csrf_token="csrf",
        companies=[],
        contact={**contact, "archived_at": "2026-01-01"},
    )
    assert _archive_button_markup(edit_restore, "Restore contact")


@pytest.mark.unit
def test_admin_action_btn_css_resets_native_button_appearance() -> None:
    css = _admin_css()
    base = _rule_block(css, ".admin-action-btn {")
    destructive = _rule_block(css, ".admin-action-btn--destructive {")
    secondary = _rule_block(css, ".admin-action-btn--secondary {")
    disabled = _rule_block(css, ".admin-action-btn:disabled {")

    for block in (destructive, secondary):
        assert "background:" in block
        assert "border-color:" in block
        assert "color:" in block

    assert "appearance: none" in base
    assert "cursor: pointer" in base
    assert "border-radius:" in base
    assert "padding:" in base
    assert "font-family: inherit" in base
    assert "color:" in base

    assert "outline:" in _rule_block(css, ".admin-action-btn:focus-visible {")
    assert "outline-color:" in _rule_block(css, ".admin-action-btn--destructive:focus-visible {")
    assert ":disabled" in disabled
    assert "cursor: not-allowed" in disabled
    assert "opacity:" in disabled


@pytest.mark.unit
def test_archive_buttons_do_not_use_primary_cta_classes() -> None:
    html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme"},
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    button = _archive_button_markup(html, "Archive company")
    assert button is not None
    assert "cta" not in button
    assert "admin-submit" not in button


@pytest.mark.unit
def test_preview_archive_restore_detail_pages_render_action_buttons(
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

    cookies = {SESSION_COOKIE_NAME: "preview-screenshot-session"}
    company_archive = client.get(
        f"/admin/companies/{PREVIEW_COMPANY_ARCHIVE_ID}",
        cookies=cookies,
    )
    assert company_archive.status_code == 200
    assert _archive_button_markup(company_archive.text, "Archive company")

    company_restore = client.get(
        f"/admin/companies/{PREVIEW_COMPANY_RESTORE_ID}",
        cookies=cookies,
    )
    assert company_restore.status_code == 200
    assert _archive_button_markup(company_restore.text, "Restore company")

    contact_archive = client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_ARCHIVE_ID}",
        cookies=cookies,
    )
    assert contact_archive.status_code == 200
    assert _archive_button_markup(contact_archive.text, "Archive contact")

    contact_restore = client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_RESTORE_ID}",
        cookies=cookies,
    )
    assert contact_restore.status_code == 200
    assert _archive_button_markup(contact_restore.text, "Restore contact")

    contact_edit_archive = client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_ARCHIVE_ID}/edit",
        cookies=cookies,
    )
    assert contact_edit_archive.status_code == 200
    assert _archive_button_markup(contact_edit_archive.text, "Archive contact")

    contact_edit_restore = client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_RESTORE_ID}/edit",
        cookies=cookies,
    )
    assert contact_edit_restore.status_code == 200
    assert _archive_button_markup(contact_edit_restore.text, "Restore contact")


@pytest.mark.unit
def test_preview_archive_restore_fixtures_stable_with_seed() -> None:
    a = preview_company_detail(PREVIEW_COMPANY_ARCHIVE_ID, rng=random.Random(42))
    b = preview_company_detail(PREVIEW_COMPANY_ARCHIVE_ID, rng=random.Random(42))
    assert a == b
    assert a is not None
    assert a["archived_at"] is None

    archived = preview_company_detail(PREVIEW_COMPANY_RESTORE_ID, rng=random.Random(42))
    assert archived is not None
    assert archived["archived_at"] is not None

    contact_a = preview_contact_detail(PREVIEW_CONTACT_ARCHIVE_ID, rng=random.Random(7))
    contact_b = preview_contact_detail(PREVIEW_CONTACT_ARCHIVE_ID, rng=random.Random(7))
    assert contact_a == contact_b
    assert contact_a is not None
    assert contact_a[0]["archived_at"] is None

    restored = preview_contact_detail(PREVIEW_CONTACT_RESTORE_ID, rng=random.Random(7))
    assert restored is not None
    assert restored[0]["archived_at"] is not None
