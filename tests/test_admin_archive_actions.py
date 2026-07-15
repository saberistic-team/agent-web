"""Tests for themed Archive/Restore admin action buttons (#233)."""

from __future__ import annotations

import random
import re
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app import admin_contacts, admin_research_pages
from app.admin_auth import SESSION_COOKIE_NAME
from app.admin_layout import render_admin_archive_form
from app.admin_preview import (
    PREVIEW_CRM_COMPANY_ACTIVE_ID,
    PREVIEW_CRM_COMPANY_ARCHIVED_ID,
    PREVIEW_CRM_CONTACT_ACTIVE_ID,
    PREVIEW_CRM_CONTACT_ARCHIVED_ID,
    preview_company_crm_detail,
    preview_contact_crm_detail,
    preview_contact_crm_edit,
)
from app.main import app

ADMIN_CSS = Path(__file__).resolve().parents[1] / "site/assets/admin.css"

COMPANY_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
CONTACT_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

client = TestClient(app, follow_redirects=False)


def _archive_button_classes(html: str, label: str) -> str:
    match = re.search(
        rf'<button[^>]*class="([^"]*)"[^>]*>\s*{re.escape(label)}\s*</button>',
        html,
    )
    assert match is not None, f"Expected button labeled {label!r}"
    return match.group(1)


@pytest.mark.unit
def test_render_admin_archive_form_uses_semantic_classes() -> None:
    archive_html = render_admin_archive_form(
        form_action="/admin/companies/example",
        label="Archive company",
        archived_at=None,
        csrf_token="csrf",
    )
    assert 'class="admin-action admin-action--destructive"' in archive_html
    assert 'action="/admin/companies/example/archive"' in archive_html

    restore_html = render_admin_archive_form(
        form_action="/admin/contacts/example",
        label="Restore contact",
        archived_at="2026-01-01",
        csrf_token="csrf",
    )
    assert 'class="admin-action admin-action--secondary"' in restore_html
    assert 'action="/admin/contacts/example/restore"' in restore_html


@pytest.mark.unit
def test_company_research_page_archive_and_restore_markup() -> None:
    archive_html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme"},
        contacts=[],
        records=[],
        csrf_token="csrf",
    )
    assert "Archive company" in archive_html
    assert "admin-exit" not in _archive_button_classes(archive_html, "Archive company")
    assert "admin-action--destructive" in archive_html

    restore_html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme", "archived_at": "2026-01-01"},
        contacts=[],
        records=[],
        csrf_token="csrf",
    )
    assert "Restore company" in restore_html
    assert "admin-action--secondary" in restore_html
    assert "admin-exit" not in _archive_button_classes(restore_html, "Restore company")


@pytest.mark.unit
def test_contact_research_and_edit_pages_use_semantic_archive_actions() -> None:
    detail_html = admin_research_pages.render_admin_contact_research_page(
        contact={"id": CONTACT_ID, "full_name": "Pat", "buying_roles": []},
        company=None,
        records=[],
        csrf_token="csrf",
    )
    assert "Archive contact" in detail_html
    assert "admin-action--destructive" in detail_html

    edit_html = admin_contacts.render_contact_form_page(
        admin_username="operator",
        csrf_token="csrf",
        companies=[],
        contact={"id": CONTACT_ID, "full_name": "Pat"},
    )
    assert "Archive contact" in edit_html
    assert "admin-action--destructive" in edit_html
    assert 'class="admin-action admin-action--destructive"' in edit_html

    archived_edit_html = admin_contacts.render_contact_form_page(
        admin_username="operator",
        csrf_token="csrf",
        companies=[],
        contact={"id": CONTACT_ID, "full_name": "Pat", "archived_at": "2026-01-01"},
    )
    assert "Restore contact" in archived_edit_html
    assert "admin-action--secondary" in archived_edit_html


@pytest.mark.unit
def test_admin_action_css_resets_native_button_appearance() -> None:
    css = ADMIN_CSS.read_text(encoding="utf-8")
    action_block = css.split(".admin-action {", 1)[1].split("}", 1)[0]
    for prop in (
        "background:",
        "border:",
        "padding:",
        "font-family:",
        "color:",
        "cursor:",
        "border-radius:",
    ):
        assert prop in action_block, f"Missing {prop} on .admin-action"
    assert "background: none" not in action_block
    assert "#fff" not in action_block.lower()
    assert "buttonface" not in action_block.lower()

    assert ".admin-action:focus-visible" in css
    assert ".admin-action:active" in css
    assert ".admin-action:disabled" in css
    assert ".admin-action--destructive" in css
    assert ".admin-action--secondary" in css


@pytest.mark.unit
def test_admin_action_variants_use_brand_tokens_not_primary_cta() -> None:
    css = ADMIN_CSS.read_text(encoding="utf-8")
    destructive_block = css.split(".admin-action--destructive {", 1)[1].split("}", 1)[0]
    secondary_block = css.split(".admin-action--secondary {", 1)[1].split("}", 1)[0]
    assert "var(--accent)" not in destructive_block
    assert "background: var(--accent)" not in secondary_block
    assert "#e05a5a" in destructive_block or "e05a5a" in destructive_block


@pytest.mark.unit
def test_preview_crm_detail_seed_stable() -> None:
    a = preview_company_crm_detail(archived=False, rng=random.Random(42))
    b = preview_company_crm_detail(archived=False, rng=random.Random(42))
    assert a == b
    archived = preview_company_crm_detail(archived=True, rng=random.Random(42))
    assert archived[0]["archived_at"] is not None
    assert a[0]["archived_at"] is None

    contact_a = preview_contact_crm_detail(archived=False, rng=random.Random(7))
    contact_b = preview_contact_crm_detail(archived=False, rng=random.Random(7))
    assert contact_a == contact_b

    edit_a = preview_contact_crm_edit(rng=random.Random(9))
    edit_b = preview_contact_crm_edit(rng=random.Random(9))
    assert edit_a == edit_b


@pytest.mark.unit
@pytest.mark.integration
def test_preview_routes_render_archive_and_restore_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "42")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cookie = {SESSION_COOKIE_NAME: "preview-screenshot-session"}

    company_archive = client.get(
        f"/admin/companies/{PREVIEW_CRM_COMPANY_ACTIVE_ID}",
        cookies=cookie,
    )
    assert company_archive.status_code == 200
    assert "Archive company" in company_archive.text
    assert "admin-action--destructive" in company_archive.text

    company_restore = client.get(
        f"/admin/companies/{PREVIEW_CRM_COMPANY_ARCHIVED_ID}",
        cookies=cookie,
    )
    assert company_restore.status_code == 200
    assert "Restore company" in company_restore.text
    assert "admin-action--secondary" in company_restore.text

    contact_archive = client.get(
        f"/admin/contacts/{PREVIEW_CRM_CONTACT_ACTIVE_ID}",
        cookies=cookie,
    )
    assert contact_archive.status_code == 200
    assert "Archive contact" in contact_archive.text
    assert "admin-action--destructive" in contact_archive.text

    contact_restore = client.get(
        f"/admin/contacts/{PREVIEW_CRM_CONTACT_ARCHIVED_ID}",
        cookies=cookie,
    )
    assert contact_restore.status_code == 200
    assert "Restore contact" in contact_restore.text
    assert "admin-action--secondary" in contact_restore.text

    contact_edit = client.get(
        f"/admin/contacts/{PREVIEW_CRM_CONTACT_ACTIVE_ID}/edit",
        cookies=cookie,
    )
    assert contact_edit.status_code == 200
    assert "Archive contact" in contact_edit.text
    assert "admin-action--destructive" in contact_edit.text
