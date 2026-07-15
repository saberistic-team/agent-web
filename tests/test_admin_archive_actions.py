"""Regression tests for themed archive/restore admin action buttons (#233)."""

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
from app.admin_layout import render_admin_archive_action_form
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


def _archive_button_class(html: str, label: str) -> str:
    match = re.search(
        rf'<button class="([^"]+)" type="submit">{re.escape(label)}</button>',
        html,
    )
    assert match is not None, f"missing archive/restore button for {label!r}"
    return match.group(1)


@pytest.mark.unit
def test_render_admin_archive_action_form_emits_semantic_classes() -> None:
    archive = render_admin_archive_action_form(
        action_path="/admin/companies/1/archive",
        label="Archive company",
        csrf_token="csrf",
        variant="destructive",
    )
    assert 'class="admin-action admin-action--destructive"' in archive
    assert 'action="/admin/companies/1/archive"' in archive
    assert "Archive company" in archive

    restore = render_admin_archive_action_form(
        action_path="/admin/companies/1/restore",
        label="Restore company",
        csrf_token="csrf",
        variant="secondary",
    )
    assert 'class="admin-action admin-action--secondary"' in restore


@pytest.mark.unit
def test_admin_action_css_resets_native_button_appearance() -> None:
    css = _admin_css()
    block = _rule_block(css, ".admin-action {")
    assert "background:" in block
    assert "border:" in block
    assert "padding:" in block
    assert "font-family: inherit" in block
    assert "color:" in block
    assert "cursor: pointer" in block
    assert "border-radius:" in block
    assert "appearance:" not in block


@pytest.mark.unit
def test_admin_action_css_includes_interaction_and_disabled_states() -> None:
    css = _admin_css()
    assert ".admin-action:hover" in css
    assert ".admin-action:focus-visible" in css
    assert ".admin-action:active" in css
    assert ".admin-action:disabled" in css
    disabled_block = _rule_block(css, ".admin-action:disabled")
    assert "cursor: not-allowed" in disabled_block
    assert "opacity:" in disabled_block


@pytest.mark.unit
def test_admin_action_variants_differ_from_primary_cta() -> None:
    css = _admin_css()
    destructive = _rule_block(css, ".admin-action--destructive {")
    secondary = _rule_block(css, ".admin-action--secondary {")
    assert "var(--accent)" not in destructive
    assert "#e88a6a" in destructive
    assert "color: var(--muted)" in secondary


@pytest.mark.unit
def test_company_research_page_archive_and_restore_markup() -> None:
    archive_html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme"},
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert _archive_button_class(archive_html, "Archive company") == (
        "admin-action admin-action--destructive"
    )
    assert 'class="admin-exit" type="submit">Archive company' not in archive_html

    restore_html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme", "archived_at": "2026-07-01"},
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert _archive_button_class(restore_html, "Restore company") == (
        "admin-action admin-action--secondary"
    )


@pytest.mark.unit
def test_contact_research_and_edit_archive_and_restore_markup() -> None:
    contact = {
        "id": CONTACT_ID,
        "full_name": "Ada",
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
    assert _archive_button_class(detail_archive, "Archive contact") == (
        "admin-action admin-action--destructive"
    )

    detail_restore = admin_research_pages.render_admin_contact_research_page(
        contact={**contact, "archived_at": "2026-07-01"},
        company={"id": COMPANY_ID, "name": "Acme"},
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert _archive_button_class(detail_restore, "Restore contact") == (
        "admin-action admin-action--secondary"
    )

    edit_archive = admin_contacts.render_contact_form_page(
        csrf_token="csrf",
        admin_username="operator",
        companies=[{"id": COMPANY_ID, "name": "Acme"}],
        contact=contact,
    )
    assert _archive_button_class(edit_archive, "Archive contact") == (
        "admin-action admin-action--destructive"
    )

    edit_restore = admin_contacts.render_contact_form_page(
        csrf_token="csrf",
        admin_username="operator",
        companies=[],
        contact={**contact, "archived_at": "2026-07-01"},
    )
    assert _archive_button_class(edit_restore, "Restore contact") == (
        "admin-action admin-action--secondary"
    )


@pytest.mark.unit
def test_admin_css_asset_served_with_action_button_rules() -> None:
    response = client.get("/assets/admin.css")
    assert response.status_code == 200
    body = response.text
    assert ".admin-action {" in body
    assert ".admin-action--destructive" in body
    assert ".admin-action--secondary" in body


@pytest.mark.unit
def test_preview_company_and_contact_archive_states_seed_stable() -> None:
    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    company_archive_a = build_preview_company_research(
        PREVIEW_COMPANY_ARCHIVE_ID, rng=random.Random(42), now=now
    )
    company_archive_b = build_preview_company_research(
        PREVIEW_COMPANY_ARCHIVE_ID, rng=random.Random(42), now=now
    )
    assert company_archive_a == company_archive_b
    assert company_archive_a is not None
    assert company_archive_a["company"]["archived_at"] is None

    company_restore = build_preview_company_research(
        PREVIEW_COMPANY_RESTORE_ID, rng=random.Random(42), now=now
    )
    assert company_restore is not None
    assert company_restore["company"]["archived_at"] is not None

    contact_archive = build_preview_contact_research(
        PREVIEW_CONTACT_ARCHIVE_ID, rng=random.Random(42), now=now
    )
    contact_restore = build_preview_contact_research(
        PREVIEW_CONTACT_RESTORE_ID, rng=random.Random(42), now=now
    )
    assert contact_archive is not None and contact_archive["contact"]["archived_at"] is None
    assert contact_restore is not None and contact_restore["contact"]["archived_at"] is not None

    edit = build_preview_contact_edit(
        PREVIEW_CONTACT_ARCHIVE_ID, rng=random.Random(42), now=now
    )
    assert edit is not None
    assert edit["companies"]


@pytest.mark.unit
def test_preview_routes_render_themed_archive_and_restore_buttons(
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

    company_archive = client.get(
        f"/admin/companies/{PREVIEW_COMPANY_ARCHIVE_ID}",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert company_archive.status_code == 200
    assert _archive_button_class(company_archive.text, "Archive company") == (
        "admin-action admin-action--destructive"
    )

    company_restore = client.get(
        f"/admin/companies/{PREVIEW_COMPANY_RESTORE_ID}",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert company_restore.status_code == 200
    assert _archive_button_class(company_restore.text, "Restore company") == (
        "admin-action admin-action--secondary"
    )

    contact_archive = client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_ARCHIVE_ID}",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert contact_archive.status_code == 200
    assert _archive_button_class(contact_archive.text, "Archive contact") == (
        "admin-action admin-action--destructive"
    )

    contact_restore = client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_RESTORE_ID}",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert contact_restore.status_code == 200
    assert _archive_button_class(contact_restore.text, "Restore contact") == (
        "admin-action admin-action--secondary"
    )

    edit_archive = client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_ARCHIVE_ID}/edit",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert edit_archive.status_code == 200
    assert _archive_button_class(edit_archive.text, "Archive contact") == (
        "admin-action admin-action--destructive"
    )

    edit_restore = client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_RESTORE_ID}/edit",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert edit_restore.status_code == 200
    assert _archive_button_class(edit_restore.text, "Restore contact") == (
        "admin-action admin-action--secondary"
    )
