"""Regression tests for themed Archive/Restore admin action buttons (#233)."""

from __future__ import annotations

import re
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app import admin_contacts, admin_research_pages
from app.admin_auth import SESSION_COOKIE_NAME
from app.admin_layout import render_admin_archive_button
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


def _archive_button_class(html: str, label: str) -> str | None:
    match = re.search(
        rf'<button class="([^"]+)" type="submit">{re.escape(label)}</button>',
        html,
    )
    return match.group(1) if match else None


@pytest.mark.unit
def test_render_admin_archive_button_uses_semantic_classes() -> None:
    archive = render_admin_archive_button(label="Archive company", archived_at=None)
    restore = render_admin_archive_button(
        label="Restore company",
        archived_at="2026-01-01",
    )
    assert 'class="admin-action-btn admin-action-btn--destructive"' in archive
    assert "Archive company" in archive
    assert 'class="admin-action-btn admin-action-btn--secondary"' in restore
    assert "Restore company" in restore
    assert "admin-exit" not in archive
    assert "admin-exit" not in restore


@pytest.mark.unit
def test_admin_action_btn_resets_native_button_appearance() -> None:
    css = _admin_css()
    block = _rule_block(css, ".admin-action-btn {")
    assert "font-family: inherit" in block
    assert "background:" in block
    assert "border:" in block
    assert "padding:" in block
    assert "color:" in block
    assert "cursor: pointer" in block
    assert "border-radius:" in block


@pytest.mark.unit
def test_admin_action_btn_states_cover_interaction_and_disabled() -> None:
    css = _admin_css()
    assert ".admin-action-btn:hover" in css
    assert ".admin-action-btn:focus-visible" in css
    assert "outline: 2px solid var(--accent)" in _rule_block(css, ".admin-action-btn:focus-visible")
    assert ".admin-action-btn:active" in css
    assert ".admin-action-btn:disabled" in css
    assert "cursor: not-allowed" in _rule_block(css, ".admin-action-btn:disabled,")
    assert "opacity: 0.5" in _rule_block(css, ".admin-action-btn:disabled,")


@pytest.mark.unit
def test_admin_action_btn_variants_are_visually_distinct() -> None:
    css = _admin_css()
    destructive = _rule_block(css, ".admin-action-btn--destructive {")
    secondary = _rule_block(css, ".admin-action-btn--secondary {")
    assert "#e05a5a" in destructive
    assert "#ffb4b4" in destructive
    assert "var(--ink)" in secondary
    assert destructive != secondary


@pytest.mark.unit
def test_company_research_page_archive_and_restore_markup() -> None:
    archive_html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme"},
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    restore_html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme", "archived_at": "2026-01-01"},
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert _archive_button_class(archive_html, "Archive company") == (
        "admin-action-btn admin-action-btn--destructive"
    )
    assert _archive_button_class(restore_html, "Restore company") == (
        "admin-action-btn admin-action-btn--secondary"
    )
    assert 'class="admin-exit" type="submit">Archive' not in archive_html
    assert 'class="cta admin-submit"' in archive_html


@pytest.mark.unit
def test_contact_research_and_edit_pages_archive_and_restore_markup() -> None:
    contact = {
        "id": CONTACT_ID,
        "full_name": "Pat",
        "company_id": COMPANY_ID,
        "buying_roles": [],
    }
    archived_contact = {**contact, "archived_at": "2026-01-01"}
    detail_archive = admin_research_pages.render_admin_contact_research_page(
        contact=contact,
        company={"id": COMPANY_ID, "name": "Acme"},
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    detail_restore = admin_research_pages.render_admin_contact_research_page(
        contact=archived_contact,
        company={"id": COMPANY_ID, "name": "Acme"},
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    edit_archive = admin_contacts.render_contact_form_page(
        admin_username="operator",
        csrf_token="csrf",
        companies=[{"id": COMPANY_ID, "name": "Acme"}],
        contact=contact,
    )
    edit_restore = admin_contacts.render_contact_form_page(
        admin_username="operator",
        csrf_token="csrf",
        companies=[{"id": COMPANY_ID, "name": "Acme"}],
        contact=archived_contact,
    )
    for html, label, variant in (
        (detail_archive, "Archive contact", "admin-action-btn--destructive"),
        (detail_restore, "Restore contact", "admin-action-btn--secondary"),
        (edit_archive, "Archive contact", "admin-action-btn--destructive"),
        (edit_restore, "Restore contact", "admin-action-btn--secondary"),
    ):
        classes = _archive_button_class(html, label)
        assert classes == f"admin-action-btn {variant}"


@pytest.mark.unit
def test_admin_css_asset_served_with_action_button_rules() -> None:
    response = client.get("/assets/admin.css")
    assert response.status_code == 200
    body = response.text
    assert ".admin-action-btn {" in body
    assert ".admin-action-btn--destructive" in body
    assert ".admin-action-btn--secondary" in body
    assert "cursor: pointer" in body


@pytest.mark.unit
@pytest.mark.parametrize(
    ("route", "label", "variant"),
    (
        (
            f"/admin/companies/{PREVIEW_COMPANY_ARCHIVE_ID}",
            "Archive company",
            "admin-action-btn--destructive",
        ),
        (
            f"/admin/companies/{PREVIEW_COMPANY_RESTORE_ID}",
            "Restore company",
            "admin-action-btn--secondary",
        ),
        (
            f"/admin/contacts/{PREVIEW_CONTACT_ARCHIVE_ID}",
            "Archive contact",
            "admin-action-btn--destructive",
        ),
        (
            f"/admin/contacts/{PREVIEW_CONTACT_RESTORE_ID}",
            "Restore contact",
            "admin-action-btn--secondary",
        ),
        (
            f"/admin/contacts/{PREVIEW_CONTACT_ARCHIVE_ID}/edit",
            "Archive contact",
            "admin-action-btn--destructive",
        ),
        (
            f"/admin/contacts/{PREVIEW_CONTACT_RESTORE_ID}/edit",
            "Restore contact",
            "admin-action-btn--secondary",
        ),
    ),
)
def test_preview_detail_and_edit_pages_render_themed_archive_buttons(
    monkeypatch: pytest.MonkeyPatch,
    route: str,
    label: str,
    variant: str,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "42")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    response = client.get(
        route,
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert response.status_code == 200
    classes = _archive_button_class(response.text, label)
    assert classes == f"admin-action-btn {variant}"
    assert 'class="admin-exit" type="submit"' not in response.text
