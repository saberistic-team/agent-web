"""Regression tests for themed Archive/Restore admin action buttons (#233)."""

from __future__ import annotations

import random
import re
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app import admin_contacts, admin_research_pages
from app.admin_layout import render_admin_archive_form
from app.admin_preview import (
    PREVIEW_CRM_COMPANY_ACTIVE_ID,
    PREVIEW_CRM_COMPANY_ARCHIVED_ID,
    PREVIEW_CRM_CONTACT_ACTIVE_ID,
    PREVIEW_CRM_CONTACT_ARCHIVED_ID,
    preview_company_research_detail,
    preview_contact_edit_detail,
    preview_contact_research_detail,
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


def _archive_button(html: str) -> str:
    match = re.search(
        r'<button class="admin-action[^"]*" type="submit">[^<]+</button>',
        html,
    )
    assert match is not None, "expected archive/restore action button"
    return match.group(0)


@pytest.mark.unit
def test_render_admin_archive_form_uses_destructive_variant_when_active() -> None:
    html = render_admin_archive_form(
        resource_path="/admin/companies/acme",
        csrf_token="csrf",
        archived_at=None,
        archive_label="Archive company",
        restore_label="Restore company",
    )
    assert 'class="admin-action admin-action--destructive"' in html
    assert "Archive company" in html
    assert 'action="/admin/companies/acme/archive"' in html
    assert "admin-exit" not in html


@pytest.mark.unit
def test_render_admin_archive_form_uses_secondary_variant_when_archived() -> None:
    html = render_admin_archive_form(
        resource_path="/admin/contacts/pat",
        csrf_token="csrf",
        archived_at="2026-01-01",
        archive_label="Archive contact",
        restore_label="Restore contact",
    )
    assert 'class="admin-action admin-action--secondary"' in html
    assert "Restore contact" in html
    assert 'action="/admin/contacts/pat/restore"' in html


@pytest.mark.unit
def test_company_research_page_renders_themed_archive_button() -> None:
    html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme", "archived_at": None},
        contacts=[],
        records=[],
        csrf_token="csrf",
    )
    button = _archive_button(html)
    assert "admin-action--destructive" in button
    assert "Archive company" in button
    assert 'class="admin-exit" type="submit">Archive company' not in html


@pytest.mark.unit
def test_company_research_page_renders_themed_restore_button() -> None:
    html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme", "archived_at": "2026-01-01"},
        contacts=[],
        records=[],
        csrf_token="csrf",
    )
    button = _archive_button(html)
    assert "admin-action--secondary" in button
    assert "Restore company" in button


@pytest.mark.unit
def test_contact_research_and_edit_pages_render_themed_archive_buttons() -> None:
    contact = {
        "id": CONTACT_ID,
        "full_name": "Pat",
        "company_id": COMPANY_ID,
        "buying_roles": [],
        "archived_at": None,
    }
    research_html = admin_research_pages.render_admin_contact_research_page(
        contact=contact,
        company={"id": COMPANY_ID, "name": "Acme"},
        records=[],
        csrf_token="csrf",
    )
    assert "admin-action--destructive" in _archive_button(research_html)

    edit_html = admin_contacts.render_contact_form_page(
        admin_username="operator",
        csrf_token="csrf",
        companies=[{"id": COMPANY_ID, "name": "Acme"}],
        contact=contact,
    )
    assert "admin-action--destructive" in _archive_button(edit_html)

    archived = {**contact, "archived_at": "2026-01-01"}
    restore_html = admin_contacts.render_contact_form_page(
        admin_username="operator",
        csrf_token="csrf",
        companies=[{"id": COMPANY_ID, "name": "Acme"}],
        contact=archived,
    )
    assert "admin-action--secondary" in _archive_button(restore_html)
    assert "Restore contact" in restore_html


@pytest.mark.unit
def test_admin_action_css_resets_native_button_appearance() -> None:
    css = _admin_css()
    base = _rule_block(css, ".admin-action {")
    assert "appearance: none" in base
    assert "cursor: pointer" in base
    assert "background:" in base
    assert "border:" in base
    assert "border-radius:" in base
    assert "padding:" in base
    assert "font-family: inherit" in base
    assert "color:" in base

    destructive = _rule_block(css, ".admin-action--destructive {")
    assert "border-color:" in destructive
    assert "#b85c5c" in destructive

    secondary = _rule_block(css, ".admin-action--secondary {")
    assert "border-color: var(--line)" in secondary

    disabled = _rule_block(css, ".admin-action:disabled,")
    assert "cursor: not-allowed" in disabled
    assert "opacity:" in disabled

    focus = _rule_block(css, ".admin-action:focus-visible {")
    assert "box-shadow:" in focus
    assert "outline: none" in focus


@pytest.mark.unit
def test_admin_action_css_includes_interaction_states() -> None:
    css = _admin_css()
    assert ".admin-action:hover {" in css
    assert ".admin-action:active {" in css
    assert ".admin-action--destructive:hover {" in css
    assert ".admin-action--secondary:hover {" in css
    assert ".admin-action--destructive:focus-visible {" in css
    assert ".admin-action--secondary:focus-visible {" in css


@pytest.mark.unit
def test_preview_company_and_contact_detail_fixtures_include_archive_states() -> None:
    active_company = preview_company_research_detail(PREVIEW_CRM_COMPANY_ACTIVE_ID, rng=random.Random(11))
    archived_company = preview_company_research_detail(PREVIEW_CRM_COMPANY_ARCHIVED_ID, rng=random.Random(11))
    active_contact = preview_contact_research_detail(PREVIEW_CRM_CONTACT_ACTIVE_ID, rng=random.Random(11))
    archived_contact = preview_contact_research_detail(PREVIEW_CRM_CONTACT_ARCHIVED_ID, rng=random.Random(11))

    assert active_company is not None
    assert archived_company is not None
    assert active_contact is not None
    assert archived_contact is not None
    assert active_company["company"]["archived_at"] is None  # type: ignore[index]
    assert archived_company["company"]["archived_at"] is not None  # type: ignore[index]
    assert active_contact["contact"]["archived_at"] is None  # type: ignore[index]
    assert archived_contact["contact"]["archived_at"] is not None  # type: ignore[index]

    active_edit = preview_contact_edit_detail(PREVIEW_CRM_CONTACT_ACTIVE_ID, rng=random.Random(11))
    archived_edit = preview_contact_edit_detail(PREVIEW_CRM_CONTACT_ARCHIVED_ID, rng=random.Random(11))
    assert active_edit is not None
    assert archived_edit is not None


@pytest.mark.unit
@pytest.mark.integration
def test_preview_routes_render_themed_archive_and_restore_buttons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", "preview-admin")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", "unused")
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "preview-session-secret-32chars-minimum")

    from app.admin_auth import reset_login_rate_limiter

    reset_login_rate_limiter()

    headers = {"Cookie": "admin_session=preview-screenshot-session"}

    company_archive = client.get(
        f"/admin/companies/{PREVIEW_CRM_COMPANY_ACTIVE_ID}",
        headers=headers,
    )
    assert company_archive.status_code == 200
    assert "admin-action--destructive" in company_archive.text
    assert "Archive company" in company_archive.text

    company_restore = client.get(
        f"/admin/companies/{PREVIEW_CRM_COMPANY_ARCHIVED_ID}",
        headers=headers,
    )
    assert company_restore.status_code == 200
    assert "admin-action--secondary" in company_restore.text
    assert "Restore company" in company_restore.text

    contact_archive = client.get(
        f"/admin/contacts/{PREVIEW_CRM_CONTACT_ACTIVE_ID}",
        headers=headers,
    )
    assert contact_archive.status_code == 200
    assert "admin-action--destructive" in contact_archive.text

    contact_restore = client.get(
        f"/admin/contacts/{PREVIEW_CRM_CONTACT_ARCHIVED_ID}",
        headers=headers,
    )
    assert contact_restore.status_code == 200
    assert "admin-action--secondary" in contact_restore.text

    contact_edit_archive = client.get(
        f"/admin/contacts/{PREVIEW_CRM_CONTACT_ACTIVE_ID}/edit",
        headers=headers,
    )
    assert contact_edit_archive.status_code == 200
    assert "admin-action--destructive" in contact_edit_archive.text

    contact_edit_restore = client.get(
        f"/admin/contacts/{PREVIEW_CRM_CONTACT_ARCHIVED_ID}/edit",
        headers=headers,
    )
    assert contact_edit_restore.status_code == 200
    assert "admin-action--secondary" in contact_edit_restore.text
