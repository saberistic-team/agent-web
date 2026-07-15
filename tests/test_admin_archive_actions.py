"""Regression tests for archive/restore admin action button styling (#233)."""

from __future__ import annotations

import random
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app import admin_contacts, admin_research_pages
from app.admin_auth import SESSION_COOKIE_NAME
from app.admin_preview import (
    PREVIEW_CRM_COMPANY_ACTIVE_ID,
    PREVIEW_CRM_COMPANY_ARCHIVED_ID,
    PREVIEW_CRM_CONTACT_ACTIVE_ID,
    PREVIEW_CRM_CONTACT_ARCHIVED_ID,
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


def _archive_button_class(html: str, label: str) -> str:
    match = re.search(
        rf'<button class="([^"]+)" type="submit">{re.escape(label)}</button>',
        html,
    )
    assert match is not None, f"Missing archive/restore button for {label!r}"
    return match.group(1)


@pytest.mark.unit
def test_admin_action_css_resets_native_button_appearance() -> None:
    css = _admin_css()
    block = _rule_block(css, ".admin-action {")
    assert "appearance: none" in block
    assert "-webkit-appearance: none" in block
    assert "background:" in block
    assert "border:" in block
    assert "padding:" in block
    assert "cursor: pointer" in block
    assert "border-radius:" in block
    assert "font-family: inherit" in block
    assert "color: var(--ink)" in block


@pytest.mark.unit
def test_admin_action_css_includes_interaction_and_disabled_states() -> None:
    css = _admin_css()
    assert ".admin-action:hover" in css
    assert ".admin-action:focus-visible" in css
    assert ".admin-action:active:not(:disabled):not([aria-disabled=\"true\"])" in css
    disabled_block = _rule_block(css, ".admin-action:disabled,")
    assert "opacity: 0.55" in disabled_block
    assert "cursor: not-allowed" in disabled_block


@pytest.mark.unit
def test_admin_action_destructive_and_secondary_variants_differ_from_primary() -> None:
    css = _admin_css()
    destructive = _rule_block(css, ".admin-action--destructive {")
    secondary = _rule_block(css, ".admin-action--secondary {")
    assert "#e88a6a" in destructive
    assert "var(--accent)" in secondary
    assert "background: var(--accent)" not in destructive
    assert "background: var(--accent)" not in secondary
    assert ".admin-action--destructive:focus-visible" in css
    assert ".admin-action--secondary:focus-visible" in css


@pytest.mark.unit
def test_admin_css_asset_served_with_admin_action_rules() -> None:
    response = client.get("/assets/admin.css")
    assert response.status_code == 200
    body = response.text
    assert ".admin-action {" in body
    assert ".admin-action--destructive" in body
    assert ".admin-action--secondary" in body


@pytest.mark.unit
def test_company_research_archive_and_restore_use_semantic_action_classes() -> None:
    active_html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme"},
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert _archive_button_class(active_html, "Archive company") == (
        "admin-action admin-action--destructive"
    )
    assert 'class="admin-exit" type="submit">Archive company' not in active_html

    archived_html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme", "archived_at": "2026-01-01"},
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert _archive_button_class(archived_html, "Restore company") == (
        "admin-action admin-action--secondary"
    )


@pytest.mark.unit
def test_contact_research_archive_and_restore_use_semantic_action_classes() -> None:
    active_html = admin_research_pages.render_admin_contact_research_page(
        contact={"id": CONTACT_ID, "full_name": "Pat", "buying_roles": []},
        company=None,
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert _archive_button_class(active_html, "Archive contact") == (
        "admin-action admin-action--destructive"
    )

    archived_html = admin_research_pages.render_admin_contact_research_page(
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
    assert _archive_button_class(archived_html, "Restore contact") == (
        "admin-action admin-action--secondary"
    )


@pytest.mark.unit
def test_contact_edit_archive_and_restore_use_semantic_action_classes() -> None:
    active_html = admin_contacts.render_contact_form_page(
        admin_username="operator",
        csrf_token="csrf",
        companies=[],
        contact={"id": CONTACT_ID, "full_name": "Pat"},
    )
    assert _archive_button_class(active_html, "Archive contact") == (
        "admin-action admin-action--destructive"
    )

    archived_html = admin_contacts.render_contact_form_page(
        admin_username="operator",
        csrf_token="csrf",
        companies=[],
        contact={"id": CONTACT_ID, "full_name": "Pat", "archived_at": "2026-01-01"},
    )
    assert _archive_button_class(archived_html, "Restore contact") == (
        "admin-action admin-action--secondary"
    )


@pytest.mark.unit
def test_preview_company_detail_seed_stable() -> None:
    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    a = build_preview_company_detail(
        PREVIEW_CRM_COMPANY_ACTIVE_ID,
        rng=random.Random(42),
        now=now,
    )
    b = build_preview_company_detail(
        PREVIEW_CRM_COMPANY_ACTIVE_ID,
        rng=random.Random(42),
        now=now,
    )
    assert a == b
    assert a is not None
    company, contacts, records = a
    assert company.get("archived_at") is None
    assert contacts
    assert records


@pytest.mark.unit
def test_preview_company_archived_state_exposes_restore_action() -> None:
    preview = build_preview_company_detail(PREVIEW_CRM_COMPANY_ARCHIVED_ID, rng=random.Random(7))
    assert preview is not None
    company, _contacts, _records = preview
    assert company.get("archived_at") is not None


@pytest.mark.unit
def test_preview_contact_detail_seed_stable() -> None:
    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    a = build_preview_contact_detail(
        PREVIEW_CRM_CONTACT_ACTIVE_ID,
        rng=random.Random(42),
        now=now,
    )
    b = build_preview_contact_detail(
        PREVIEW_CRM_CONTACT_ACTIVE_ID,
        rng=random.Random(42),
        now=now,
    )
    assert a == b
    assert a is not None
    contact, company, records = a
    assert contact.get("archived_at") is None
    assert company is not None
    assert records


@pytest.mark.unit
@pytest.mark.integration
def test_preview_company_routes_render_archive_and_restore_buttons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "42")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    archive_response = client.get(
        f"/admin/companies/{PREVIEW_CRM_COMPANY_ACTIVE_ID}",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert archive_response.status_code == 200
    assert 'class="admin-action admin-action--destructive"' in archive_response.text
    assert "Archive company" in archive_response.text

    restore_response = client.get(
        f"/admin/companies/{PREVIEW_CRM_COMPANY_ARCHIVED_ID}",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert restore_response.status_code == 200
    assert 'class="admin-action admin-action--secondary"' in restore_response.text
    assert "Restore company" in restore_response.text


@pytest.mark.unit
@pytest.mark.integration
def test_preview_contact_routes_render_archive_and_restore_buttons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "42")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    archive_response = client.get(
        f"/admin/contacts/{PREVIEW_CRM_CONTACT_ACTIVE_ID}",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert archive_response.status_code == 200
    assert 'class="admin-action admin-action--destructive"' in archive_response.text
    assert "Archive contact" in archive_response.text

    restore_response = client.get(
        f"/admin/contacts/{PREVIEW_CRM_CONTACT_ARCHIVED_ID}/edit",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert restore_response.status_code == 200
    assert 'class="admin-action admin-action--secondary"' in restore_response.text
    assert "Restore contact" in restore_response.text
