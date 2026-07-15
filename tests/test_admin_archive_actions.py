"""Regression tests for themed archive/restore admin action buttons (#233)."""

from __future__ import annotations

import re
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app import admin_contacts, admin_research_pages
from app.admin_auth import SESSION_COOKIE_NAME
from app.admin_preview import (
    PREVIEW_COMPANY_ARCHIVE_ID,
    PREVIEW_COMPANY_RESTORE_ID,
    PREVIEW_CONTACT_ARCHIVE_ID,
    PREVIEW_CONTACT_RESTORE_ID,
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
        rf'<button class="admin-action admin-action--(?:destructive|restore)" '
        rf'type="submit">{re.escape(label)}</button>'
    )
    match = re.search(pattern, html)
    return match.group(0) if match else None


@pytest.mark.unit
def test_admin_action_css_resets_native_button_appearance() -> None:
    css = _admin_css()
    base = _rule_block(css, ".admin-action {")
    assert "background:" in base
    assert "border:" in base
    assert "padding:" in base
    assert "font-family:" in base
    assert "color:" in base
    assert "cursor: pointer" in base
    assert "border-radius:" in base
    assert "background: none" not in base
    assert "background: white" not in base.lower()
    assert "background: #fff" not in base.lower()

    assert ".admin-action:hover" in css
    assert ".admin-action:focus-visible" in css
    assert ".admin-action:active" in css
    assert ".admin-action:disabled" in css
    assert ".admin-action--destructive" in css
    assert ".admin-action--restore" in css
    assert ".admin-action--destructive:focus-visible" in css
    assert ".admin-action--restore:focus-visible" in css


@pytest.mark.unit
def test_company_detail_archive_and_restore_use_semantic_classes() -> None:
    archive_html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme"},
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert _archive_button_markup(archive_html, "Archive company")
    assert 'class="admin-exit" type="submit">Archive company' not in archive_html

    restore_html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme", "archived_at": "2026-07-01"},
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert _archive_button_markup(restore_html, "Restore company")
    assert 'admin-action--restore' in restore_html
    assert 'class="admin-exit" type="submit">Restore company' not in restore_html


@pytest.mark.unit
def test_contact_detail_and_edit_archive_and_restore_use_semantic_classes() -> None:
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
        contact={**contact, "archived_at": "2026-07-01"},
        company={"id": COMPANY_ID, "name": "Acme"},
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert _archive_button_markup(detail_restore, "Restore contact")

    edit_archive = admin_contacts.render_contact_form_page(
        admin_username="operator",
        csrf_token="csrf",
        companies=[{"id": COMPANY_ID, "name": "Acme"}],
        contact=contact,
    )
    assert _archive_button_markup(edit_archive, "Archive contact")
    assert 'class="cta admin-submit" type="submit">Save contact' in edit_archive

    edit_restore = admin_contacts.render_contact_form_page(
        admin_username="operator",
        csrf_token="csrf",
        companies=[{"id": COMPANY_ID, "name": "Acme"}],
        contact={**contact, "archived_at": "2026-07-01"},
    )
    assert _archive_button_markup(edit_restore, "Restore contact")


@pytest.mark.unit
def test_preview_crm_detail_pages_render_archive_restore_buttons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "11")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    cookies = {SESSION_COOKIE_NAME: "preview-screenshot-session"}

    company_archive_page = client.get(
        f"/admin/companies/{PREVIEW_COMPANY_ARCHIVE_ID}",
        cookies=cookies,
    )
    assert company_archive_page.status_code == 200
    assert 'admin-action--destructive' in company_archive_page.text
    assert "Archive company" in company_archive_page.text
    assert "No research records yet." not in company_archive_page.text

    company_restore_page = client.get(
        f"/admin/companies/{PREVIEW_COMPANY_RESTORE_ID}",
        cookies=cookies,
    )
    assert company_restore_page.status_code == 200
    assert 'admin-action--restore' in company_restore_page.text
    assert "Restore company" in company_restore_page.text
    assert "No research records yet." not in company_restore_page.text

    contact_archive_page = client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_ARCHIVE_ID}",
        cookies=cookies,
    )
    assert contact_archive_page.status_code == 200
    assert 'admin-action--destructive' in contact_archive_page.text
    assert "Archive contact" in contact_archive_page.text
    assert "No research records yet." not in contact_archive_page.text

    contact_restore_page = client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_RESTORE_ID}",
        cookies=cookies,
    )
    assert contact_restore_page.status_code == 200
    assert 'admin-action--restore' in contact_restore_page.text
    assert "Restore contact" in contact_restore_page.text
    assert "No research records yet." not in contact_restore_page.text

    contact_edit_restore = client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_RESTORE_ID}/edit",
        cookies=cookies,
    )
    assert contact_edit_restore.status_code == 200
    assert 'admin-action--restore' in contact_edit_restore.text
    assert "Restore contact" in contact_edit_restore.text
    assert "Save contact" in contact_edit_restore.text
