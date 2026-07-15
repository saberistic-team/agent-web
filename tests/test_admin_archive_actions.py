"""Regression tests for themed Archive/Restore admin action buttons (#233)."""

from __future__ import annotations

import random
from pathlib import Path
from uuid import UUID

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app import admin_contacts, admin_research_pages
from app.admin_auth import SESSION_COOKIE_NAME
from app.admin_layout import render_archive_action_button
from app.admin_preview import (
    PREVIEW_COMPANY_ARCHIVE_ID,
    PREVIEW_COMPANY_RESTORE_ID,
    PREVIEW_CONTACT_ARCHIVE_ID,
    PREVIEW_CONTACT_RESTORE_ID,
    preview_company_research_detail,
    preview_contact_edit,
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


def _preview_env(monkeypatch: pytest.MonkeyPatch) -> None:
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


@pytest.mark.unit
def test_render_archive_action_button_uses_semantic_classes() -> None:
    archive = render_archive_action_button(label="Archive company", is_restore=False)
    restore = render_archive_action_button(label="Restore company", is_restore=True)
    assert 'class="admin-action-btn admin-action-btn--destructive"' in archive
    assert 'class="admin-action-btn admin-action-btn--restore"' in restore
    assert "admin-exit" not in archive
    assert "admin-exit" not in restore


@pytest.mark.unit
def test_admin_action_btn_resets_native_button_appearance() -> None:
    css = _admin_css()
    block = _rule_block(css, ".admin-action-btn {")
    assert "appearance: none" in block
    assert "-webkit-appearance: none" in block
    assert "background:" in block
    assert "border:" in block
    assert "padding:" in block
    assert "font-family: inherit" in block
    assert "color:" in block
    assert "cursor: pointer" in block
    assert "border-radius:" in block


@pytest.mark.unit
def test_admin_action_btn_states_cover_interaction_and_disabled() -> None:
    css = _admin_css()
    assert ".admin-action-btn:hover" in css
    assert ".admin-action-btn:focus-visible" in css
    assert ".admin-action-btn:active:not(:disabled)" in css
    assert ".admin-action-btn:disabled" in css
    assert ".admin-action-btn--destructive:hover" in css
    assert ".admin-action-btn--destructive:focus-visible" in css
    assert ".admin-action-btn--destructive:disabled" in css
    assert ".admin-action-btn--restore:hover" in css
    assert ".admin-action-btn--restore:disabled" in css

    destructive = _rule_block(css, ".admin-action-btn--destructive {")
    assert "#ffb4b4" in destructive
    assert "background:" in destructive

    disabled = _rule_block(css, ".admin-action-btn:disabled {")
    assert "opacity: 0.55" in disabled
    assert "cursor: not-allowed" in disabled


@pytest.mark.unit
def test_company_research_page_renders_themed_archive_and_restore_buttons() -> None:
    archive_html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme"},
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert 'class="admin-action-btn admin-action-btn--destructive"' in archive_html
    assert "Archive company" in archive_html
    assert 'action="/admin/companies/' in archive_html
    assert '<button class="admin-exit"' not in archive_html

    restore_html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme", "archived_at": "2026-07-01"},
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert 'class="admin-action-btn admin-action-btn--restore"' in restore_html
    assert "Restore company" in restore_html


@pytest.mark.unit
def test_contact_pages_render_themed_archive_and_restore_buttons() -> None:
    detail_html = admin_research_pages.render_admin_contact_research_page(
        contact={"id": CONTACT_ID, "full_name": "Pat", "buying_roles": []},
        company=None,
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert 'class="admin-action-btn admin-action-btn--destructive"' in detail_html
    assert "Archive contact" in detail_html

    edit_html = admin_contacts.render_contact_form_page(
        admin_username="operator",
        csrf_token="csrf",
        companies=[],
        contact={"id": CONTACT_ID, "full_name": "Pat", "archived_at": "2026-07-01"},
    )
    assert 'class="admin-action-btn admin-action-btn--restore"' in edit_html
    assert "Restore contact" in edit_html
    assert '<button class="admin-exit"' not in edit_html


@pytest.mark.unit
def test_archive_buttons_remain_distinct_from_primary_save_actions() -> None:
    html = admin_contacts.render_contact_form_page(
        admin_username="operator",
        csrf_token="csrf",
        companies=[{"id": COMPANY_ID, "name": "Acme"}],
        contact={"id": CONTACT_ID, "full_name": "Pat"},
    )
    assert 'class="cta admin-submit"' in html
    assert 'class="admin-action-btn admin-action-btn--destructive"' in html
    assert 'class="cta admin-submit" type="submit">Save contact' in html


@pytest.mark.unit
def test_admin_css_asset_includes_action_button_rules() -> None:
    response = client.get("/assets/admin.css")
    assert response.status_code == 200
    body = response.text
    assert ".admin-action-btn--destructive" in body
    assert ".admin-action-btn--restore" in body
    assert "appearance: none" in body


@pytest.mark.unit
def test_preview_company_detail_pages_include_archive_and_restore_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _preview_env(monkeypatch)
    archive = preview_company_research_detail(PREVIEW_COMPANY_ARCHIVE_ID, rng=random.Random(42))
    restore = preview_company_research_detail(PREVIEW_COMPANY_RESTORE_ID, rng=random.Random(42))
    assert archive is not None
    assert restore is not None
    assert archive[0]["archived_at"] is None
    assert restore[0]["archived_at"] is not None

    archive_page = client.get(
        f"/admin/companies/{PREVIEW_COMPANY_ARCHIVE_ID}",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    restore_page = client.get(
        f"/admin/companies/{PREVIEW_COMPANY_RESTORE_ID}",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert archive_page.status_code == 200
    assert restore_page.status_code == 200
    assert 'class="admin-action-btn admin-action-btn--destructive"' in archive_page.text
    assert 'class="admin-action-btn admin-action-btn--restore"' in restore_page.text
    assert archive[0]["name"] in archive_page.text
    assert restore[0]["name"] in restore_page.text


@pytest.mark.unit
def test_preview_contact_detail_and_edit_include_archive_and_restore_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _preview_env(monkeypatch)
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "7")
    archive = preview_contact_research_detail(PREVIEW_CONTACT_ARCHIVE_ID, rng=random.Random(7))
    edit = preview_contact_edit(PREVIEW_CONTACT_RESTORE_ID, rng=random.Random(7))
    assert archive is not None
    assert edit is not None

    archive_page = client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_ARCHIVE_ID}",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    restore_edit_page = client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_RESTORE_ID}/edit",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert archive_page.status_code == 200
    assert restore_edit_page.status_code == 200
    assert 'class="admin-action-btn admin-action-btn--destructive"' in archive_page.text
    assert 'class="admin-action-btn admin-action-btn--restore"' in restore_edit_page.text
    assert archive[0]["full_name"] in archive_page.text
    assert edit[0]["full_name"] in restore_edit_page.text
