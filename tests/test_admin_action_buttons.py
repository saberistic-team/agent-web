"""Regression tests for themed admin archive/restore action buttons (#233)."""

from __future__ import annotations

import re
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app import admin_contacts, admin_research_pages
from app.admin_auth import SESSION_COOKIE_NAME
from app.admin_layout import render_admin_archive_form
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


def _archive_button_classes(html: str) -> list[str]:
    return re.findall(
        r'<button class="([^"]*admin-action-btn[^"]*)" type="submit">',
        html,
    )


@pytest.mark.unit
def test_render_admin_archive_form_uses_semantic_action_classes() -> None:
    archive = render_admin_archive_form(
        entity_base_path="/admin/companies/test-id",
        entity_label="company",
        archived_at=None,
        csrf_token="csrf",
    )
    restore = render_admin_archive_form(
        entity_base_path="/admin/companies/test-id",
        entity_label="company",
        archived_at="2026-01-01",
        csrf_token="csrf",
    )
    assert 'class="admin-action-btn admin-action-btn--destructive"' in archive
    assert "Archive company" in archive
    assert 'action="/admin/companies/test-id/archive"' in archive
    assert 'class="admin-action-btn admin-action-btn--restore"' in restore
    assert "Restore company" in restore
    assert 'action="/admin/companies/test-id/restore"' in restore
    assert "admin-exit" not in archive
    assert "admin-exit" not in restore


@pytest.mark.unit
def test_company_research_page_archive_and_restore_markup() -> None:
    active_html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme"},
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    archived_html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme", "archived_at": "2026-01-01"},
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert _archive_button_classes(active_html) == ["admin-action-btn admin-action-btn--destructive"]
    assert _archive_button_classes(archived_html) == ["admin-action-btn admin-action-btn--restore"]
    assert 'class="admin-exit" type="submit"' not in active_html
    assert 'class="admin-exit" type="submit"' not in archived_html


@pytest.mark.unit
def test_contact_pages_archive_and_restore_markup() -> None:
    contact = {
        "id": CONTACT_ID,
        "full_name": "Pat Example",
        "company_id": COMPANY_ID,
        "buying_roles": [],
    }
    detail_html = admin_research_pages.render_admin_contact_research_page(
        contact=contact,
        company={"id": COMPANY_ID, "name": "Acme"},
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    edit_html = admin_contacts.render_contact_form_page(
        admin_username="operator",
        csrf_token="csrf",
        companies=[{"id": COMPANY_ID, "name": "Acme"}],
        contact=contact,
    )
    restore_detail = admin_research_pages.render_admin_contact_research_page(
        contact={**contact, "archived_at": "2026-01-01"},
        company={"id": COMPANY_ID, "name": "Acme"},
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    restore_edit = admin_contacts.render_contact_form_page(
        admin_username="operator",
        csrf_token="csrf",
        companies=[{"id": COMPANY_ID, "name": "Acme"}],
        contact={**contact, "archived_at": "2026-01-01"},
    )
    assert _archive_button_classes(detail_html) == ["admin-action-btn admin-action-btn--destructive"]
    assert _archive_button_classes(edit_html) == ["admin-action-btn admin-action-btn--destructive"]
    assert _archive_button_classes(restore_detail) == ["admin-action-btn admin-action-btn--restore"]
    assert _archive_button_classes(restore_edit) == ["admin-action-btn admin-action-btn--restore"]


@pytest.mark.unit
def test_admin_action_btn_resets_native_button_appearance() -> None:
    css = _admin_css()
    base = _rule_block(css, ".admin-action-btn {")
    destructive = _rule_block(css, ".admin-action-btn--destructive {")
    restore = _rule_block(css, ".admin-action-btn--restore {")
    assert "background:" in base
    assert "border:" in base
    assert "padding:" in base
    assert "cursor: pointer" in base
    assert "border-radius:" in base
    assert "color:" in base
    assert "font-family:" in base
    assert "background:" in destructive
    assert "border-color:" in destructive
    assert "background:" in restore
    assert "border-color:" in restore
    assert ":focus-visible" in css
    assert ".admin-action-btn:disabled" in css
    assert ":active:not(:disabled)" in css
    assert "#e05a5a" in destructive
    assert "admin-exit" not in base


@pytest.mark.unit
def test_archive_buttons_remain_distinct_from_primary_cta() -> None:
    html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme"},
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert 'class="cta admin-submit"' in html
    assert 'class="admin-action-btn admin-action-btn--destructive"' in html
    assert html.index("admin-action-btn") < html.index("cta admin-submit")


@pytest.mark.integration
def test_preview_archive_restore_routes_render_themed_buttons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from argon2 import PasswordHasher

    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "42")
    monkeypatch.setenv("ADMIN_USERNAME", "preview-admin")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", PasswordHasher().hash("preview"))
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "preview-session-secret-32chars-minimum")
    cookies = {SESSION_COOKIE_NAME: "preview-screenshot-session"}

    company_archive = client.get(
        f"/admin/companies/{PREVIEW_COMPANY_ARCHIVE_ID}",
        cookies=cookies,
    )
    company_restore = client.get(
        f"/admin/companies/{PREVIEW_COMPANY_RESTORE_ID}",
        cookies=cookies,
    )
    contact_archive = client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_ARCHIVE_ID}",
        cookies=cookies,
    )
    contact_restore = client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_RESTORE_ID}",
        cookies=cookies,
    )
    contact_edit_archive = client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_ARCHIVE_ID}/edit",
        cookies=cookies,
    )
    contact_edit_restore = client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_RESTORE_ID}/edit",
        cookies=cookies,
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
        assert "admin-action-btn" in response.text
        assert 'class="admin-exit" type="submit"' not in response.text

    assert "admin-action-btn--destructive" in company_archive.text
    assert "Archive company" in company_archive.text
    assert "admin-action-btn--restore" in company_restore.text
    assert "Restore company" in company_restore.text
    assert "admin-action-btn--destructive" in contact_edit_archive.text
    assert "Archive contact" in contact_edit_archive.text
    assert "admin-action-btn--restore" in contact_edit_restore.text
    assert "Restore contact" in contact_edit_restore.text
