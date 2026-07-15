"""Regression tests for themed Archive/Restore admin action buttons (#233)."""

from __future__ import annotations

import random
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app import admin_contacts, admin_research_pages
from app.admin_auth import SESSION_COOKIE_NAME
from app.admin_layout import archive_action_button_class
from app.admin_preview import (
    PREVIEW_COMPANY_ACTIVE_ID,
    PREVIEW_COMPANY_ARCHIVED_ID,
    PREVIEW_CONTACT_ACTIVE_ID,
    PREVIEW_CONTACT_ARCHIVED_ID,
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


@pytest.mark.unit
def test_archive_action_button_class_variants() -> None:
    assert archive_action_button_class(archived=False) == (
        "admin-action admin-action--destructive"
    )
    assert archive_action_button_class(archived=True) == "admin-action admin-action--restore"


@pytest.mark.unit
def test_admin_action_css_resets_native_button_appearance() -> None:
    css = _admin_css()
    base = _rule_block(css, ".admin-action {")
    assert "appearance: none" in base
    assert "background:" in base
    assert "border:" in base
    assert "padding:" in base
    assert "font-family: inherit" in base
    assert "border-radius:" in base
    assert "cursor: pointer" in base
    assert "color:" in base
    assert "background: none" not in base

    destructive = _rule_block(css, ".admin-action--destructive {")
    restore = _rule_block(css, ".admin-action--restore {")
    assert "background:" in destructive
    assert "border-color:" in destructive
    assert "color:" in destructive
    assert "background:" in restore
    assert "border-color:" in restore
    assert "color:" in restore

    focus = _rule_block(css, ".admin-action:focus-visible {")
    disabled = _rule_block(css, ".admin-action:disabled {")
    assert "outline:" in focus
    assert "opacity:" in disabled
    assert "cursor: not-allowed" in disabled


@pytest.mark.unit
def test_company_research_archive_and_restore_buttons_use_semantic_classes() -> None:
    archive_html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme"},
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert 'class="admin-action admin-action--destructive"' in archive_html
    assert '<button class="admin-exit" type="submit">Archive company</button>' not in archive_html

    restore_html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme", "archived_at": "2026-07-01"},
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert 'class="admin-action admin-action--restore"' in restore_html
    assert '<button class="admin-exit" type="submit">Restore company</button>' not in restore_html


@pytest.mark.unit
def test_contact_research_and_edit_archive_buttons_use_semantic_classes() -> None:
    research_archive = admin_research_pages.render_admin_contact_research_page(
        contact={"id": CONTACT_ID, "full_name": "Ada", "buying_roles": []},
        company=None,
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert 'class="admin-action admin-action--destructive"' in research_archive
    assert '<button class="admin-exit" type="submit">Archive contact</button>' not in research_archive

    research_restore = admin_research_pages.render_admin_contact_research_page(
        contact={
            "id": CONTACT_ID,
            "full_name": "Ada",
            "buying_roles": [],
            "archived_at": "2026-07-01",
        },
        company=None,
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert 'class="admin-action admin-action--restore"' in research_restore

    edit_archive = admin_contacts.render_contact_form_page(
        csrf_token="csrf",
        admin_username="operator",
        companies=[],
        contact={"id": CONTACT_ID, "full_name": "Ada"},
    )
    assert 'class="admin-action admin-action--destructive"' in edit_archive
    assert '<button class="admin-exit" type="submit">Archive contact</button>' not in edit_archive

    edit_restore = admin_contacts.render_contact_form_page(
        csrf_token="csrf",
        admin_username="operator",
        companies=[],
        contact={"id": CONTACT_ID, "full_name": "Ada", "archived_at": "2026-07-01"},
    )
    assert 'class="admin-action admin-action--restore"' in edit_restore


@pytest.mark.unit
def test_preview_company_and_contact_detail_seed_stable() -> None:
    from datetime import datetime, timezone

    fixed_now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    now = build_preview_company_detail(
        PREVIEW_COMPANY_ACTIVE_ID,
        rng=random.Random(9),
        now=fixed_now,
    )
    again = build_preview_company_detail(
        PREVIEW_COMPANY_ACTIVE_ID,
        rng=random.Random(9),
        now=fixed_now,
    )
    assert now == again
    assert now is not None
    company, contacts, records = now
    assert company["archived_at"] is None
    assert contacts
    assert records

    archived = build_preview_company_detail(
        PREVIEW_COMPANY_ARCHIVED_ID,
        rng=random.Random(9),
        now=fixed_now,
    )
    assert archived is not None
    assert archived[0]["archived_at"] is not None

    contact_active = build_preview_contact_detail(
        PREVIEW_CONTACT_ACTIVE_ID,
        rng=random.Random(9),
        now=fixed_now,
    )
    contact_archived = build_preview_contact_detail(
        PREVIEW_CONTACT_ARCHIVED_ID,
        rng=random.Random(9),
        now=fixed_now,
    )
    assert contact_active is not None
    assert contact_archived is not None
    assert contact_active[0]["archived_at"] is None
    assert contact_archived[0]["archived_at"] is not None


@pytest.mark.unit
@pytest.mark.integration
def test_preview_routes_render_archive_and_restore_action_buttons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "42")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cookie = {SESSION_COOKIE_NAME: "preview-screenshot-session"}

    company_archive = client.get(
        f"/admin/companies/{PREVIEW_COMPANY_ACTIVE_ID}",
        cookies=cookie,
    )
    assert company_archive.status_code == 200
    assert 'admin-action--destructive' in company_archive.text
    assert "Archive company" in company_archive.text

    company_restore = client.get(
        f"/admin/companies/{PREVIEW_COMPANY_ARCHIVED_ID}",
        cookies=cookie,
    )
    assert company_restore.status_code == 200
    assert 'admin-action--restore' in company_restore.text
    assert "Restore company" in company_restore.text

    contact_archive = client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_ACTIVE_ID}",
        cookies=cookie,
    )
    assert contact_archive.status_code == 200
    assert 'admin-action--destructive' in contact_archive.text
    assert "Archive contact" in contact_archive.text

    contact_restore = client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_ARCHIVED_ID}",
        cookies=cookie,
    )
    assert contact_restore.status_code == 200
    assert 'admin-action--restore' in contact_restore.text
    assert "Restore contact" in contact_restore.text

    contact_edit_archive = client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_ACTIVE_ID}/edit",
        cookies=cookie,
    )
    assert contact_edit_archive.status_code == 200
    assert 'admin-action--destructive' in contact_edit_archive.text

    contact_edit_restore = client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_ARCHIVED_ID}/edit",
        cookies=cookie,
    )
    assert contact_edit_restore.status_code == 200
    assert 'admin-action--restore' in contact_edit_restore.text
