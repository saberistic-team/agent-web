"""Regression tests for themed Archive/Restore admin action buttons (#233)."""

from __future__ import annotations

import random
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import admin_contacts, admin_research_pages
from app.admin_auth import SESSION_COOKIE_NAME
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

COMPANY_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
CONTACT_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

client = TestClient(app, follow_redirects=False)


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


def _archive_button_markup(html: str, label: str) -> str:
    match = re.search(
        rf'<button class="([^"]+)" type="submit">{re.escape(label)}</button>',
        html,
    )
    assert match is not None, f"Expected archive button {label!r} in HTML"
    return match.group(1)


@pytest.mark.unit
def test_company_detail_archive_button_uses_destructive_action_class() -> None:
    html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme"},
        contacts=[],
        records=[],
        csrf_token="csrf",
    )
    classes = _archive_button_markup(html, "Archive company")
    assert classes == "admin-action admin-action--destructive"
    assert "admin-exit" not in classes


@pytest.mark.unit
def test_company_detail_restore_button_uses_restore_action_class() -> None:
    html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme", "archived_at": "2026-01-01"},
        contacts=[],
        records=[],
        csrf_token="csrf",
    )
    classes = _archive_button_markup(html, "Restore company")
    assert classes == "admin-action admin-action--restore"


@pytest.mark.unit
def test_contact_detail_archive_and_restore_action_classes() -> None:
    contact = {"id": CONTACT_ID, "full_name": "Pat Example"}
    archive_html = admin_research_pages.render_admin_contact_research_page(
        contact=contact,
        company=None,
        records=[],
        csrf_token="csrf",
    )
    assert (
        _archive_button_markup(archive_html, "Archive contact")
        == "admin-action admin-action--destructive"
    )

    restore_html = admin_research_pages.render_admin_contact_research_page(
        contact={**contact, "archived_at": "2026-01-01"},
        company=None,
        records=[],
        csrf_token="csrf",
    )
    assert (
        _archive_button_markup(restore_html, "Restore contact")
        == "admin-action admin-action--restore"
    )


@pytest.mark.unit
def test_contact_edit_archive_and_restore_action_classes() -> None:
    archive_html = admin_contacts.render_contact_form_page(
        admin_username="operator",
        csrf_token="csrf",
        companies=[],
        contact={"id": CONTACT_ID, "full_name": "Pat"},
    )
    assert (
        _archive_button_markup(archive_html, "Archive contact")
        == "admin-action admin-action--destructive"
    )

    restore_html = admin_contacts.render_contact_form_page(
        admin_username="operator",
        csrf_token="csrf",
        companies=[],
        contact={"id": CONTACT_ID, "full_name": "Pat", "archived_at": "2026-01-01"},
    )
    assert (
        _archive_button_markup(restore_html, "Restore contact")
        == "admin-action admin-action--restore"
    )


@pytest.mark.unit
def test_admin_action_css_resets_native_button_appearance() -> None:
    css = ADMIN_CSS.read_text(encoding="utf-8")
    base = _rule_block(css, ".admin-action {")
    assert "background:" in base
    assert "border:" in base
    assert "padding:" in base
    assert "font-family: inherit" in base
    assert "cursor: pointer" in base
    assert "border-radius:" in base
    assert "color:" in base

    destructive = _rule_block(css, ".admin-action--destructive {")
    assert "background:" in destructive
    assert destructive != base

    restore = _rule_block(css, ".admin-action--restore {")
    assert "background:" in restore
    assert restore != destructive

    assert ".admin-action:focus-visible" in css
    assert ".admin-action:active" in css
    assert ".admin-action:disabled" in css
    assert ".admin-action--destructive:disabled" in css
    assert ".admin-action--restore:disabled" in css


@pytest.mark.unit
def test_admin_action_css_avoids_native_white_button_defaults() -> None:
    css = ADMIN_CSS.read_text(encoding="utf-8")
    base = _rule_block(css, ".admin-action {")
    assert "background: none" not in base
    assert "background: transparent" not in base
    assert "background: color-mix" in base or "background: var(" in base


@pytest.mark.unit
@pytest.mark.integration
def test_preview_company_detail_renders_archive_and_restore_buttons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "42")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    archive = client.get(
        f"/admin/companies/{PREVIEW_COMPANY_ARCHIVE_ID}",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    restore = client.get(
        f"/admin/companies/{PREVIEW_COMPANY_RESTORE_ID}",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert archive.status_code == 200
    assert restore.status_code == 200
    assert 'class="admin-action admin-action--destructive"' in archive.text
    assert "Archive company" in archive.text
    assert 'class="admin-action admin-action--restore"' in restore.text
    assert "Restore company" in restore.text
    company, _contacts, _records = preview_company_detail(
        PREVIEW_COMPANY_ARCHIVE_ID,
        rng=random.Random(42),
    )
    assert company["name"] in archive.text


@pytest.mark.unit
@pytest.mark.integration
def test_preview_contact_detail_and_edit_render_archive_restore_buttons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "42")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    archive_detail = client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_ARCHIVE_ID}",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    restore_detail = client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_RESTORE_ID}",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    archive_edit = client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_ARCHIVE_ID}/edit",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    restore_edit = client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_RESTORE_ID}/edit",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert archive_detail.status_code == 200
    assert restore_detail.status_code == 200
    assert archive_edit.status_code == 200
    assert restore_edit.status_code == 200
    assert 'class="admin-action admin-action--destructive"' in archive_detail.text
    assert 'class="admin-action admin-action--restore"' in restore_detail.text
    assert 'class="admin-action admin-action--destructive"' in archive_edit.text
    assert 'class="admin-action admin-action--restore"' in restore_edit.text
    contact, _company, _records = preview_contact_detail(
        PREVIEW_CONTACT_ARCHIVE_ID,
        rng=random.Random(42),
    )
    assert contact["full_name"] in archive_detail.text
