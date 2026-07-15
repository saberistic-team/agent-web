"""Archive/restore admin action button styling (#233)."""

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
    preview_company_crm_detail,
    preview_contact_crm_detail,
)
from app.main import app

ADMIN_CSS = Path(__file__).resolve().parents[1] / "site/assets/admin.css"

COMPANY_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
CONTACT_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

client = TestClient(app, follow_redirects=False)


def _rule_block(css: str, selector: str) -> str:
    start = css.index(selector)
    brace_start = css.index("{", start)
    depth = 0
    for index, char in enumerate(css[brace_start:], start=brace_start):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return css[start : index + 1]
    raise AssertionError(f"Unclosed rule for {selector!r}")


def _css_declarations(block: str) -> set[str]:
    body = block.split("{", 1)[1].rsplit("}", 1)[0]
    return {line.split(":", 1)[0].strip() for line in body.split(";") if ":" in line}


@pytest.mark.unit
def test_admin_action_btn_resets_native_button_appearance() -> None:
    css = ADMIN_CSS.read_text(encoding="utf-8")
    base = _rule_block(css, ".admin-action-btn {")
    declarations = _css_declarations(base)
    for prop in (
        "background",
        "border",
        "padding",
        "font-family",
        "color",
        "cursor",
        "border-radius",
    ):
        assert prop in declarations
    assert "admin-exit" not in base
    assert _rule_block(css, ".admin-action-btn:focus-visible {")
    assert _rule_block(css, ".admin-action-btn:disabled,")
    assert _rule_block(css, ".admin-action-btn--destructive {")
    assert _rule_block(css, ".admin-action-btn--restore {")


@pytest.mark.unit
def test_company_research_page_archive_and_restore_markup() -> None:
    archive_html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme"},
        contacts=[],
        records=[],
        csrf_token="csrf-token",
        admin_username="operator",
    )
    assert 'class="admin-action-btn admin-action-btn--destructive"' in archive_html
    assert "Archive company" in archive_html
    archive_form = re.search(
        r'<form class="admin-action-form".*?</form>',
        archive_html,
        flags=re.DOTALL,
    )
    assert archive_form is not None
    assert "admin-exit" not in archive_form.group(0)

    restore_html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme", "archived_at": "2026-01-01"},
        contacts=[],
        records=[],
        csrf_token="csrf-token",
        admin_username="operator",
    )
    assert 'class="admin-action-btn admin-action-btn--restore"' in restore_html
    assert "Restore company" in restore_html


@pytest.mark.unit
def test_contact_research_and_edit_archive_and_restore_markup() -> None:
    contact = {"id": CONTACT_ID, "full_name": "Pat", "buying_roles": []}
    detail_archive = admin_research_pages.render_admin_contact_research_page(
        contact=contact,
        company={"id": COMPANY_ID, "name": "Acme"},
        records=[],
        csrf_token="csrf-token",
        admin_username="operator",
    )
    assert 'class="admin-action-btn admin-action-btn--destructive"' in detail_archive
    assert "Archive contact" in detail_archive

    detail_restore = admin_research_pages.render_admin_contact_research_page(
        contact={**contact, "archived_at": "2026-01-01"},
        company={"id": COMPANY_ID, "name": "Acme"},
        records=[],
        csrf_token="csrf-token",
        admin_username="operator",
    )
    assert 'class="admin-action-btn admin-action-btn--restore"' in detail_restore
    assert "Restore contact" in detail_restore

    edit_archive = admin_contacts.render_contact_form_page(
        admin_username="operator",
        csrf_token="csrf-token",
        companies=[{"id": COMPANY_ID, "name": "Acme"}],
        contact=contact,
    )
    assert 'class="admin-action-btn admin-action-btn--destructive"' in edit_archive

    edit_restore = admin_contacts.render_contact_form_page(
        admin_username="operator",
        csrf_token="csrf-token",
        companies=[{"id": COMPANY_ID, "name": "Acme"}],
        contact={**contact, "archived_at": "2026-01-01"},
    )
    assert 'class="admin-action-btn admin-action-btn--restore"' in edit_restore


@pytest.mark.unit
def test_archive_buttons_do_not_use_primary_submit_classes() -> None:
    html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme"},
        contacts=[],
        records=[],
        csrf_token="csrf-token",
        admin_username="operator",
    )
    archive_block = re.search(
        r'<form class="admin-action-form".*?</form>',
        html,
        flags=re.DOTALL,
    )
    assert archive_block is not None
    block = archive_block.group(0)
    assert "admin-submit" not in block
    assert 'class="cta"' not in block


@pytest.mark.unit
def test_preview_company_and_contact_detail_archive_restore_states() -> None:
    company_archive = preview_company_crm_detail(PREVIEW_COMPANY_ARCHIVE_ID)
    company_restore = preview_company_crm_detail(PREVIEW_COMPANY_RESTORE_ID)
    assert company_archive is not None
    assert company_restore is not None
    assert company_archive["company"]["archived_at"] is None
    assert company_restore["company"]["archived_at"] is not None

    contact_archive = preview_contact_crm_detail(PREVIEW_CONTACT_ARCHIVE_ID)
    contact_restore = preview_contact_crm_detail(PREVIEW_CONTACT_RESTORE_ID)
    assert contact_archive is not None
    assert contact_restore is not None
    assert contact_archive["contact"]["archived_at"] is None
    assert contact_restore["contact"]["archived_at"] is not None


@pytest.mark.unit
@pytest.mark.integration
def test_preview_routes_render_themed_archive_restore_buttons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "42")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cookie = {SESSION_COOKIE_NAME: "preview-screenshot-session"}

    company_archive = client.get(
        f"/admin/companies/{PREVIEW_COMPANY_ARCHIVE_ID}",
        cookies=cookie,
    )
    assert company_archive.status_code == 200
    assert 'class="admin-action-btn admin-action-btn--destructive"' in company_archive.text
    assert "Archive company" in company_archive.text

    company_restore = client.get(
        f"/admin/companies/{PREVIEW_COMPANY_RESTORE_ID}",
        cookies=cookie,
    )
    assert company_restore.status_code == 200
    assert 'class="admin-action-btn admin-action-btn--restore"' in company_restore.text
    assert "Restore company" in company_restore.text

    contact_archive = client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_ARCHIVE_ID}",
        cookies=cookie,
    )
    assert contact_archive.status_code == 200
    assert 'class="admin-action-btn admin-action-btn--destructive"' in contact_archive.text

    contact_restore = client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_RESTORE_ID}",
        cookies=cookie,
    )
    assert contact_restore.status_code == 200
    assert 'class="admin-action-btn admin-action-btn--restore"' in contact_restore.text

    edit_archive = client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_ARCHIVE_ID}/edit",
        cookies=cookie,
    )
    assert edit_archive.status_code == 200
    assert 'class="admin-action-btn admin-action-btn--destructive"' in edit_archive.text

    edit_restore = client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_RESTORE_ID}/edit",
        cookies=cookie,
    )
    assert edit_restore.status_code == 200
    assert 'class="admin-action-btn admin-action-btn--restore"' in edit_restore.text
