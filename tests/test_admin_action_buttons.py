"""Regression tests for themed archive/restore admin action buttons (#233)."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app import admin_contacts, admin_research_pages
from app.admin_auth import SESSION_COOKIE_NAME
from app.admin_layout import archive_restore_button_class
from app.admin_preview import (
    PREVIEW_COMPANY_ARCHIVE_STATE_ID,
    PREVIEW_COMPANY_RESTORE_STATE_ID,
    PREVIEW_CONTACT_ARCHIVE_STATE_ID,
    PREVIEW_CONTACT_RESTORE_STATE_ID,
    build_preview_company_research_detail,
    build_preview_contact_edit_detail,
    build_preview_contact_research_detail,
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


@pytest.mark.unit
def test_archive_restore_button_class_variants() -> None:
    assert archive_restore_button_class(is_archived=False) == (
        "admin-action-btn admin-action-btn--destructive"
    )
    assert archive_restore_button_class(is_archived=True) == (
        "admin-action-btn admin-action-btn--secondary"
    )


@pytest.mark.unit
def test_admin_action_btn_resets_native_button_appearance() -> None:
    css = _admin_css()
    base = _rule_block(css, ".admin-action-btn {")
    assert "background:" in base
    assert "border:" in base
    assert "padding:" in base
    assert "font-family: inherit" in base
    assert "color:" in base
    assert "cursor: pointer" in base
    assert "border-radius:" in base
    assert "buttonface" not in base.lower()
    assert "#fff" not in base
    assert "white" not in base.split("background")[1].split("}")[0]


@pytest.mark.unit
def test_admin_action_btn_states_include_focus_hover_active_disabled() -> None:
    css = _admin_css()
    assert ".admin-action-btn:hover:not(:disabled)" in css
    assert ".admin-action-btn:focus-visible" in css
    assert ".admin-action-btn:active:not(:disabled)" in css
    assert ".admin-action-btn:disabled" in css
    disabled = _rule_block(css, ".admin-action-btn:disabled {")
    assert "cursor: not-allowed" in disabled
    assert "opacity:" in disabled
    secondary_hover = _rule_block(css, ".admin-action-btn--secondary:hover:not(:disabled) {")
    destructive_hover = _rule_block(css, ".admin-action-btn--destructive:hover:not(:disabled) {")
    assert "var(--accent)" in secondary_hover
    assert "#e05a5a" in destructive_hover


@pytest.mark.unit
def test_company_research_page_archive_and_restore_markup() -> None:
    active_html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme"},
        contacts=[],
        records=[],
        csrf_token="csrf",
    )
    assert 'class="admin-action-btn admin-action-btn--destructive"' in active_html
    assert "Archive company" in active_html
    assert 'action="/admin/companies/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/archive"' in active_html
    assert 'class="admin-exit" type="submit"' not in active_html

    archived_html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme", "archived_at": "2026-01-01"},
        contacts=[],
        records=[],
        csrf_token="csrf",
    )
    assert 'class="admin-action-btn admin-action-btn--secondary"' in archived_html
    assert "Restore company" in archived_html
    assert 'action="/admin/companies/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/restore"' in archived_html


@pytest.mark.unit
def test_contact_research_and_edit_pages_archive_and_restore_markup() -> None:
    contact = {
        "id": CONTACT_ID,
        "full_name": "Pat",
        "company_id": COMPANY_ID,
        "buying_roles": [],
    }
    detail_html = admin_research_pages.render_admin_contact_research_page(
        contact=contact,
        company={"id": COMPANY_ID, "name": "Acme"},
        records=[],
        csrf_token="csrf",
    )
    assert 'class="admin-action-btn admin-action-btn--destructive"' in detail_html
    assert "Archive contact" in detail_html

    edit_html = admin_contacts.render_contact_form_page(
        admin_username="operator",
        csrf_token="csrf",
        companies=[{"id": COMPANY_ID, "name": "Acme"}],
        contact={**contact, "archived_at": "2026-01-01"},
    )
    assert 'class="admin-action-btn admin-action-btn--secondary"' in edit_html
    assert "Restore contact" in edit_html
    assert 'class="cta admin-submit"' in edit_html


@pytest.mark.unit
def test_preview_company_and_contact_detail_archive_restore_buttons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "233")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    company_archive = client.get(
        f"/admin/companies/{PREVIEW_COMPANY_ARCHIVE_STATE_ID}",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert company_archive.status_code == 200
    assert 'class="admin-action-btn admin-action-btn--destructive"' in company_archive.text
    assert "Archive company" in company_archive.text
    assert "No research records yet." not in company_archive.text

    company_restore = client.get(
        f"/admin/companies/{PREVIEW_COMPANY_RESTORE_STATE_ID}",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert company_restore.status_code == 200
    assert 'class="admin-action-btn admin-action-btn--secondary"' in company_restore.text
    assert "Restore company" in company_restore.text

    contact_archive = client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_ARCHIVE_STATE_ID}",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert contact_archive.status_code == 200
    assert 'class="admin-action-btn admin-action-btn--destructive"' in contact_archive.text
    assert "Archive contact" in contact_archive.text

    contact_restore = client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_RESTORE_STATE_ID}",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert contact_restore.status_code == 200
    assert 'class="admin-action-btn admin-action-btn--secondary"' in contact_restore.text
    assert "Restore contact" in contact_restore.text

    contact_edit = client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_RESTORE_STATE_ID}/edit",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert contact_edit.status_code == 200
    assert 'class="admin-action-btn admin-action-btn--secondary"' in contact_edit.text
    assert "Restore contact" in contact_edit.text


@pytest.mark.unit
def test_preview_archive_restore_builders_seed_stable() -> None:
    import random
    from datetime import datetime, timezone

    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    company_a = build_preview_company_research_detail(
        PREVIEW_COMPANY_ARCHIVE_STATE_ID, rng=random.Random(233), now=now
    )
    company_b = build_preview_company_research_detail(
        PREVIEW_COMPANY_ARCHIVE_STATE_ID, rng=random.Random(233), now=now
    )
    assert company_a is not None and company_b is not None
    assert company_a[0].get("archived_at") is None
    assert company_a == company_b

    restore = build_preview_company_research_detail(
        PREVIEW_COMPANY_RESTORE_STATE_ID, rng=random.Random(233), now=now
    )
    assert restore is not None
    assert restore[0]["archived_at"] is not None

    contact_a = build_preview_contact_research_detail(
        PREVIEW_CONTACT_ARCHIVE_STATE_ID, rng=random.Random(233), now=now
    )
    contact_b = build_preview_contact_research_detail(
        PREVIEW_CONTACT_ARCHIVE_STATE_ID, rng=random.Random(233), now=now
    )
    assert contact_a is not None and contact_b is not None
    assert contact_a[0].get("archived_at") is None
    assert contact_a == contact_b

    edit = build_preview_contact_edit_detail(
        PREVIEW_CONTACT_RESTORE_STATE_ID, rng=random.Random(233), now=now
    )
    assert edit is not None
    assert edit[0]["archived_at"] is not None
