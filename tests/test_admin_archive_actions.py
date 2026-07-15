"""Archive/restore admin action button styling (#233)."""

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
from app.admin_layout import admin_archive_action_button_class
from app.admin_preview import (
    PREVIEW_COMPANY_ARCHIVE_ID,
    PREVIEW_COMPANY_RESTORE_ID,
    PREVIEW_CONTACT_ARCHIVE_ID,
    PREVIEW_CONTACT_RESTORE_ID,
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


def _archive_button_markup(html: str, label: str) -> str:
    match = re.search(
        rf'<button class="([^"]+)" type="submit">{re.escape(label)}</button>',
        html,
    )
    assert match is not None, f"Expected archive/restore button for {label!r}"
    return match.group(1)


@pytest.mark.unit
def test_admin_archive_action_button_class_variants() -> None:
    assert admin_archive_action_button_class(archived=False) == (
        "admin-action admin-action--destructive"
    )
    assert admin_archive_action_button_class(archived=True) == (
        "admin-action admin-action--restore"
    )


@pytest.mark.unit
def test_company_research_page_archive_and_restore_markup() -> None:
    archive_html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme"},
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert _archive_button_markup(archive_html, "Archive company") == (
        "admin-action admin-action--destructive"
    )
    assert 'class="admin-exit" type="submit">Archive company' not in archive_html

    restore_html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme", "archived_at": "2026-01-01"},
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert _archive_button_markup(restore_html, "Restore company") == (
        "admin-action admin-action--restore"
    )


@pytest.mark.unit
def test_contact_research_page_archive_and_restore_markup() -> None:
    archive_html = admin_research_pages.render_admin_contact_research_page(
        contact={"id": CONTACT_ID, "full_name": "Pat", "buying_roles": []},
        company={"id": COMPANY_ID, "name": "Acme"},
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert _archive_button_markup(archive_html, "Archive contact") == (
        "admin-action admin-action--destructive"
    )

    restore_html = admin_research_pages.render_admin_contact_research_page(
        contact={
            "id": CONTACT_ID,
            "full_name": "Pat",
            "buying_roles": [],
            "archived_at": "2026-01-01",
        },
        company={"id": COMPANY_ID, "name": "Acme"},
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert _archive_button_markup(restore_html, "Restore contact") == (
        "admin-action admin-action--restore"
    )


@pytest.mark.unit
def test_contact_edit_page_archive_and_restore_markup() -> None:
    archive_html = admin_contacts.render_contact_form_page(
        admin_username="operator",
        csrf_token="csrf",
        companies=[{"id": COMPANY_ID, "name": "Acme"}],
        contact={"id": CONTACT_ID, "full_name": "Pat"},
    )
    assert _archive_button_markup(archive_html, "Archive contact") == (
        "admin-action admin-action--destructive"
    )

    restore_html = admin_contacts.render_contact_form_page(
        admin_username="operator",
        csrf_token="csrf",
        companies=[],
        contact={"id": CONTACT_ID, "full_name": "Pat", "archived_at": "2026-01-01"},
    )
    assert _archive_button_markup(restore_html, "Restore contact") == (
        "admin-action admin-action--restore"
    )


@pytest.mark.unit
def test_admin_action_css_resets_native_button_appearance() -> None:
    css = _admin_css()
    base = _rule_block(css, ".admin-action {")
    destructive = _rule_block(css, ".admin-action--destructive {")
    restore = _rule_block(css, ".admin-action--restore {")

    for block in (destructive, restore):
        assert "border-color:" in block
        assert "background:" in block
        assert "color:" in block

    assert "border:" in base
    assert "appearance: none" in base
    assert "cursor: pointer" in base
    assert "border-radius:" in base
    assert "padding:" in base
    assert "font-family: inherit" in base

    assert "background: none" not in destructive
    assert "background: none" not in restore
    assert ":focus-visible" in css
    assert ":disabled" in css
    assert ":active" in css


@pytest.mark.unit
def test_admin_action_css_distinguishes_destructive_and_restore() -> None:
    css = _admin_css()
    destructive = _rule_block(css, ".admin-action--destructive {")
    restore = _rule_block(css, ".admin-action--restore {")
    assert "#e05a5a" in destructive or "#ffb4b4" in destructive
    assert "#4caf7d" in restore or "#b8f0d0" in restore
    assert destructive != restore


@pytest.mark.unit
def test_preview_archive_restore_detail_pages_render_action_buttons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "233")
    monkeypatch.setenv("ADMIN_USERNAME", "preview-admin")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", PasswordHasher().hash("preview"))
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "preview-session-secret-32chars-minimum")
    monkeypatch.setenv("BASE_URL", "http://127.0.0.1:8765")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    company_archive = client.get(f"/admin/companies/{PREVIEW_COMPANY_ARCHIVE_ID}")
    assert company_archive.status_code == 200
    assert 'class="admin-action admin-action--destructive"' in company_archive.text
    assert "Archive company" in company_archive.text

    company_restore = client.get(f"/admin/companies/{PREVIEW_COMPANY_RESTORE_ID}")
    assert company_restore.status_code == 200
    assert 'class="admin-action admin-action--restore"' in company_restore.text
    assert "Restore company" in company_restore.text

    contact_archive = client.get(f"/admin/contacts/{PREVIEW_CONTACT_ARCHIVE_ID}")
    assert contact_archive.status_code == 200
    assert 'class="admin-action admin-action--destructive"' in contact_archive.text
    assert "Archive contact" in contact_archive.text

    contact_restore = client.get(f"/admin/contacts/{PREVIEW_CONTACT_RESTORE_ID}")
    assert contact_restore.status_code == 200
    assert 'class="admin-action admin-action--restore"' in contact_restore.text
    assert "Restore contact" in contact_restore.text

    contact_edit = client.get(f"/admin/contacts/{PREVIEW_CONTACT_ARCHIVE_ID}/edit")
    assert contact_edit.status_code == 200
    assert 'class="admin-action admin-action--destructive"' in contact_edit.text
    assert "Archive contact" in contact_edit.text


@pytest.mark.unit
def test_preview_archive_restore_fixtures_seed_stable() -> None:
    now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    company_a = preview_company_research_detail(
        PREVIEW_COMPANY_ARCHIVE_ID, rng=random.Random(233), now=now
    )
    company_b = preview_company_research_detail(
        PREVIEW_COMPANY_ARCHIVE_ID, rng=random.Random(233), now=now
    )
    assert company_a is not None and company_b is not None
    assert company_a[0]["name"] == company_b[0]["name"]
    assert company_a[0].get("archived_at") is None

    restore_a = preview_company_research_detail(
        PREVIEW_COMPANY_RESTORE_ID, rng=random.Random(233), now=now
    )
    restore_b = preview_company_research_detail(
        PREVIEW_COMPANY_RESTORE_ID, rng=random.Random(233), now=now
    )
    assert restore_a is not None and restore_b is not None
    assert restore_a[0]["archived_at"] == restore_b[0]["archived_at"]

    contact_a = preview_contact_research_detail(
        PREVIEW_CONTACT_ARCHIVE_ID, rng=random.Random(233), now=now
    )
    contact_b = preview_contact_research_detail(
        PREVIEW_CONTACT_ARCHIVE_ID, rng=random.Random(233), now=now
    )
    assert contact_a is not None and contact_b is not None
    assert contact_a[0]["full_name"] == contact_b[0]["full_name"]

    edit_a = preview_contact_edit_detail(
        PREVIEW_CONTACT_RESTORE_ID, rng=random.Random(233), now=now
    )
    edit_b = preview_contact_edit_detail(
        PREVIEW_CONTACT_RESTORE_ID, rng=random.Random(233), now=now
    )
    assert edit_a is not None and edit_b is not None
    assert edit_a[0]["archived_at"] == edit_b[0]["archived_at"]
