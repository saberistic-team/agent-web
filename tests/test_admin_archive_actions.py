"""Regression tests for Archive/Restore admin action button styling (#233)."""

from __future__ import annotations

import random
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app import admin_contacts, admin_research_pages
from app.admin_auth import SESSION_COOKIE_NAME
from app.admin_layout import render_admin_archive_action_button
from app.admin_preview import (
    PREVIEW_CRM_COMPANY_ACTIVE_ID,
    PREVIEW_CRM_COMPANY_ARCHIVED_ID,
    PREVIEW_CRM_CONTACT_ACTIVE_ID,
    PREVIEW_CRM_CONTACT_ARCHIVED_ID,
    preview_crm_company_detail,
    preview_crm_contact_detail,
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
def test_render_admin_archive_action_button_uses_semantic_classes() -> None:
    archive = render_admin_archive_action_button(label="Archive company", is_restore=False)
    restore = render_admin_archive_action_button(label="Restore contact", is_restore=True)
    assert 'class="admin-action admin-action--destructive"' in archive
    assert ">Archive company</button>" in archive
    assert 'class="admin-action admin-action--restore"' in restore
    assert ">Restore contact</button>" in restore
    assert "admin-exit" not in archive
    assert "admin-exit" not in restore


@pytest.mark.unit
def test_admin_action_css_resets_native_button_appearance() -> None:
    css = _admin_css()
    base = _rule_block(css, ".admin-action {")
    assert "appearance: none" in base
    assert "-webkit-appearance: none" in base
    assert "background:" not in base.split("border:")[0]
    assert "padding: 0.55rem 0.9rem" in base
    assert "border: 1px solid transparent" in base
    assert "border-radius: 2px" in base
    assert "cursor: pointer" in base
    assert "font-family: inherit" in base


@pytest.mark.unit
def test_admin_action_destructive_and_restore_have_interactive_states() -> None:
    css = _admin_css()
    destructive = _rule_block(css, ".admin-action--destructive {")
    restore = _rule_block(css, ".admin-action--restore {")
    assert "background:" in destructive
    assert "color:" in destructive
    assert "border-color:" in destructive
    assert ".admin-action--destructive:hover" in css
    assert ".admin-action--destructive:focus-visible" in css
    assert ".admin-action--destructive:active" in css
    assert ".admin-action--destructive:disabled" in css
    assert "background:" in restore
    assert ".admin-action--restore:hover" in css
    assert ".admin-action--restore:focus-visible" in css
    assert ".admin-action--restore:active" in css
    assert ".admin-action--restore:disabled" in css
    disabled = _rule_block(css, ".admin-action--destructive:disabled {")
    assert "opacity:" in disabled
    assert "cursor: not-allowed" in disabled


@pytest.mark.unit
def test_admin_action_styles_do_not_use_native_white_button_defaults() -> None:
    css = _admin_css()
    for modifier in ("--destructive", "--restore"):
        block = _rule_block(css, f".admin-action{modifier} {{")
        assert "background: #fff" not in block
        assert "background: white" not in block
        assert "background: rgb(255" not in block
        assert "background: rgba(255" not in block


@pytest.mark.unit
def test_company_research_page_renders_themed_archive_and_restore() -> None:
    archive_html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme"},
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert 'class="admin-action admin-action--destructive"' in archive_html
    assert "Archive company" in archive_html
    assert 'action="/admin/companies/' in archive_html
    assert '<button class="admin-exit"' not in archive_html

    restore_html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme", "archived_at": "2026-01-01"},
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert 'class="admin-action admin-action--restore"' in restore_html
    assert "Restore company" in restore_html


@pytest.mark.unit
def test_contact_research_and_edit_pages_render_themed_actions() -> None:
    research_archive = admin_research_pages.render_admin_contact_research_page(
        contact={"id": CONTACT_ID, "full_name": "Ada", "buying_roles": []},
        company=None,
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert 'class="admin-action admin-action--destructive"' in research_archive
    assert "Archive contact" in research_archive

    research_restore = admin_research_pages.render_admin_contact_research_page(
        contact={
            "id": CONTACT_ID,
            "full_name": "Ada",
            "buying_roles": [],
            "archived_at": "2026-01-01",
        },
        company=None,
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert 'class="admin-action admin-action--restore"' in research_restore
    assert "Restore contact" in research_restore

    edit_archive = admin_contacts.render_contact_form_page(
        csrf_token="csrf",
        admin_username="operator",
        companies=[],
        contact={"id": CONTACT_ID, "full_name": "Ada"},
    )
    assert 'class="admin-action admin-action--destructive"' in edit_archive
    assert "Archive contact" in edit_archive

    edit_restore = admin_contacts.render_contact_form_page(
        csrf_token="csrf",
        admin_username="operator",
        companies=[],
        contact={"id": CONTACT_ID, "full_name": "Ada", "archived_at": "2026-01-01"},
    )
    assert 'class="admin-action admin-action--restore"' in edit_restore
    assert "Restore contact" in edit_restore


@pytest.mark.unit
def test_admin_css_asset_served_with_action_button_rules() -> None:
    response = client.get("/assets/admin.css")
    assert response.status_code == 200
    body = response.text
    assert ".admin-action {" in body
    assert ".admin-action--destructive" in body
    assert ".admin-action--restore" in body


@pytest.mark.unit
def test_preview_crm_detail_fixtures_seed_stable() -> None:
    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    active_company = preview_crm_company_detail(
        PREVIEW_CRM_COMPANY_ACTIVE_ID, rng=random.Random(42), now=now
    )
    archived_company = preview_crm_company_detail(
        PREVIEW_CRM_COMPANY_ARCHIVED_ID, rng=random.Random(42), now=now
    )
    assert active_company["company"]["archived_at"] is None
    assert archived_company["company"]["archived_at"] is not None
    assert active_company == preview_crm_company_detail(
        PREVIEW_CRM_COMPANY_ACTIVE_ID, rng=random.Random(42), now=now
    )

    active_contact = preview_crm_contact_detail(
        PREVIEW_CRM_CONTACT_ACTIVE_ID, rng=random.Random(7), now=now
    )
    archived_contact = preview_crm_contact_detail(
        PREVIEW_CRM_CONTACT_ARCHIVED_ID, rng=random.Random(7), now=now
    )
    assert active_contact["contact"]["archived_at"] is None
    assert archived_contact["contact"]["archived_at"] is not None


@pytest.mark.unit
@pytest.mark.integration
@pytest.mark.parametrize(
    ("path", "expected_class", "expected_label"),
    [
        (
            f"/admin/companies/{PREVIEW_CRM_COMPANY_ACTIVE_ID}",
            "admin-action--destructive",
            "Archive company",
        ),
        (
            f"/admin/companies/{PREVIEW_CRM_COMPANY_ARCHIVED_ID}",
            "admin-action--restore",
            "Restore company",
        ),
        (
            f"/admin/contacts/{PREVIEW_CRM_CONTACT_ACTIVE_ID}",
            "admin-action--destructive",
            "Archive contact",
        ),
        (
            f"/admin/contacts/{PREVIEW_CRM_CONTACT_ARCHIVED_ID}",
            "admin-action--restore",
            "Restore contact",
        ),
        (
            f"/admin/contacts/{PREVIEW_CRM_CONTACT_ACTIVE_ID}/edit",
            "admin-action--destructive",
            "Archive contact",
        ),
        (
            f"/admin/contacts/{PREVIEW_CRM_CONTACT_ARCHIVED_ID}/edit",
            "admin-action--restore",
            "Restore contact",
        ),
    ],
)
def test_preview_mode_renders_themed_archive_actions(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    expected_class: str,
    expected_label: str,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "42")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    response = client.get(path, cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"})
    assert response.status_code == 200
    body = response.text
    assert expected_label in body
    assert f'class="admin-action {expected_class}"' in body
    assert '<button class="admin-exit"' not in body
