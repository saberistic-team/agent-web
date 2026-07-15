"""Regression tests for archive/restore admin action button styling (#233)."""

from __future__ import annotations

import re
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app import admin_contacts, admin_research_pages
from app.admin_layout import admin_archive_action_class
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


def _archive_button_classes(html: str, label: str) -> str | None:
    match = re.search(
        rf'<button class="([^"]+)" type="submit">{re.escape(label)}</button>',
        html,
    )
    return match.group(1) if match else None


@pytest.mark.unit
def test_admin_archive_action_class_semantics() -> None:
    assert admin_archive_action_class(archived=False) == (
        "admin-action admin-action--destructive"
    )
    assert admin_archive_action_class(archived=True) == (
        "admin-action admin-action--secondary"
    )


@pytest.mark.unit
def test_admin_action_base_resets_native_button_appearance() -> None:
    css = _admin_css()
    block = _rule_block(css, ".admin-action {")
    for token in (
        "appearance: none",
        "background: none",
        "border: 1px solid transparent",
        "border-radius: 2px",
        "cursor: pointer",
        "font-family: inherit",
        "padding: 0.5rem 0.85rem",
    ):
        assert token in block


@pytest.mark.unit
def test_admin_action_secondary_and_destructive_have_themed_surfaces() -> None:
    css = _admin_css()
    secondary = _rule_block(css, ".admin-action--secondary {")
    destructive = _rule_block(css, ".admin-action--destructive {")
    assert "background:" in secondary
    assert "color: var(--ink)" in secondary
    assert "border-color: var(--line)" in secondary
    assert "background:" in destructive
    assert "#e05a5a" in destructive
    assert "background: #fff" not in css
    assert "background: white" not in css


@pytest.mark.unit
def test_admin_action_states_include_focus_hover_active_and_disabled() -> None:
    css = _admin_css()
    for modifier in ("secondary", "destructive"):
        assert f".admin-action--{modifier}:hover" in css
        assert f".admin-action--{modifier}:focus-visible" in css
        assert f".admin-action--{modifier}:active:not(:disabled)" in css
        assert f".admin-action--{modifier}:disabled" in css
        focus_block = _rule_block(css, f".admin-action--{modifier}:focus-visible")
        assert "outline:" in focus_block
        assert "outline-offset: 2px" in focus_block
        disabled_block = _rule_block(css, f".admin-action--{modifier}:disabled")
        assert "cursor: not-allowed" in disabled_block
        assert "opacity: 0.55" in disabled_block


@pytest.mark.unit
def test_admin_css_asset_served_with_action_button_rules() -> None:
    response = client.get("/assets/admin.css")
    assert response.status_code == 200
    body = response.text
    assert ".admin-action--destructive" in body
    assert ".admin-action--secondary" in body
    assert "appearance: none" in body


@pytest.mark.unit
def test_company_research_archive_and_restore_markup() -> None:
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
    assert (
        _archive_button_classes(archive_html, "Archive company")
        == "admin-action admin-action--destructive"
    )
    assert (
        _archive_button_classes(restore_html, "Restore company")
        == "admin-action admin-action--secondary"
    )
    assert 'action="/admin/companies/' in archive_html
    assert "/archive" in archive_html
    assert "/restore" in restore_html


@pytest.mark.unit
def test_contact_research_and_edit_archive_and_restore_markup() -> None:
    active_contact = {
        "id": CONTACT_ID,
        "full_name": "Ada",
        "company_id": COMPANY_ID,
        "buying_roles": [],
    }
    archived_contact = {**active_contact, "archived_at": "2026-01-01"}
    companies = [{"id": COMPANY_ID, "name": "Acme"}]

    research_archive = admin_research_pages.render_admin_contact_research_page(
        contact=active_contact,
        company=companies[0],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    research_restore = admin_research_pages.render_admin_contact_research_page(
        contact=archived_contact,
        company=companies[0],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    edit_archive = admin_contacts.render_contact_form_page(
        csrf_token="csrf",
        admin_username="operator",
        companies=companies,
        contact=active_contact,
    )
    edit_restore = admin_contacts.render_contact_form_page(
        csrf_token="csrf",
        admin_username="operator",
        companies=companies,
        contact=archived_contact,
    )

    for html, label, expected in (
        (research_archive, "Archive contact", "admin-action admin-action--destructive"),
        (research_restore, "Restore contact", "admin-action admin-action--secondary"),
        (edit_archive, "Archive contact", "admin-action admin-action--destructive"),
        (edit_restore, "Restore contact", "admin-action admin-action--secondary"),
    ):
        classes = _archive_button_classes(html, label)
        assert classes == expected
        assert "admin-exit" not in (classes or "")
