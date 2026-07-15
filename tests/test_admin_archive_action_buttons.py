"""Regression tests for Archive/Restore admin action button styling (#233)."""

from __future__ import annotations

import re
from pathlib import Path
from uuid import UUID

import pytest

from app import admin_contacts, admin_research_pages
from app.admin_layout import render_admin_archive_restore_button
from app.main import app
from fastapi.testclient import TestClient

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


def _archive_form_button(html: str) -> str:
    match = re.search(
        r'<form method="post" action="/admin/(?:companies|contacts)/[^"]+/(?:archive|restore)">'
        r'.*?(<button[^>]+>.*?</button>)',
        html,
        flags=re.DOTALL,
    )
    assert match is not None, "expected archive/restore form button"
    return match.group(1)


@pytest.mark.unit
def test_render_admin_archive_restore_button_variants() -> None:
    archive = render_admin_archive_restore_button(label="Archive company", is_archived=False)
    restore = render_admin_archive_restore_button(label="Restore company", is_archived=True)
    assert 'class="admin-action-btn admin-action-btn--destructive"' in archive
    assert "Archive company" in archive
    assert 'class="admin-action-btn admin-action-btn--restore"' in restore
    assert "Restore company" in restore
    assert "admin-exit" not in archive
    assert "admin-exit" not in restore


@pytest.mark.unit
def test_admin_action_btn_resets_native_button_appearance() -> None:
    css = _admin_css()
    block = _rule_block(css, ".admin-action-btn {")
    assert "appearance: none" in block
    assert "-webkit-appearance: none" in block
    assert "background: transparent" in block
    assert "border: 1px solid transparent" in block
    assert "padding: 0.55rem 0.9rem" in block
    assert "font-family: inherit" in block
    assert "border-radius: 2px" in block
    assert "cursor: pointer" in block


@pytest.mark.unit
def test_admin_action_btn_destructive_and_restore_states() -> None:
    css = _admin_css()
    destructive = _rule_block(css, ".admin-action-btn--destructive {")
    assert "border-color:" in destructive
    assert "background:" in destructive
    assert "color: #ffb4b4" in destructive

    destructive_hover = _rule_block(css, ".admin-action-btn--destructive:hover {")
    assert "border-color: #e88a6a" in destructive_hover

    destructive_focus = _rule_block(css, ".admin-action-btn--destructive:focus-visible {")
    assert "outline: 2px solid #e88a6a" in destructive_focus
    assert "outline-offset: 2px" in destructive_focus

    destructive_active = _rule_block(css, ".admin-action-btn--destructive:active {")
    assert "transform: translateY(1px)" in destructive_active

    restore = _rule_block(css, ".admin-action-btn--restore {")
    assert "border-color: var(--line)" in restore
    assert "color: var(--ink)" in restore

    restore_focus = _rule_block(css, ".admin-action-btn--restore:focus-visible {")
    assert "outline: 2px solid var(--accent)" in restore_focus

    disabled = _rule_block(css, ".admin-action-btn:disabled {")
    assert "opacity: 0.5" in disabled
    assert "cursor: not-allowed" in disabled


@pytest.mark.unit
def test_admin_action_btn_styles_do_not_use_native_white_fill() -> None:
    css = _admin_css()
    for selector in (
        ".admin-action-btn {",
        ".admin-action-btn--destructive {",
        ".admin-action-btn--restore {",
    ):
        block = _rule_block(css, selector)
        assert "background: white" not in block
        assert "background: #fff" not in block
        assert "background-color: white" not in block
        assert "background-color: #fff" not in block


@pytest.mark.unit
def test_company_research_page_renders_themed_archive_and_restore_buttons() -> None:
    active_html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme"},
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    active_button = _archive_form_button(active_html)
    assert 'class="admin-action-btn admin-action-btn--destructive"' in active_button
    assert "Archive company" in active_button
    assert "admin-exit" not in active_button

    archived_html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme", "archived_at": "2026-01-01"},
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    restore_button = _archive_form_button(archived_html)
    assert 'class="admin-action-btn admin-action-btn--restore"' in restore_button
    assert "Restore company" in restore_button
    assert 'action="/admin/companies/' in archived_html
    assert "/restore" in archived_html


@pytest.mark.unit
def test_contact_research_page_renders_themed_archive_and_restore_buttons() -> None:
    active_html = admin_research_pages.render_admin_contact_research_page(
        contact={"id": CONTACT_ID, "full_name": "Ada", "buying_roles": []},
        company={"id": COMPANY_ID, "name": "Acme"},
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    active_button = _archive_form_button(active_html)
    assert 'class="admin-action-btn admin-action-btn--destructive"' in active_button
    assert "Archive contact" in active_button

    archived_html = admin_research_pages.render_admin_contact_research_page(
        contact={
            "id": CONTACT_ID,
            "full_name": "Ada",
            "buying_roles": [],
            "archived_at": "2026-01-01",
        },
        company={"id": COMPANY_ID, "name": "Acme"},
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    restore_button = _archive_form_button(archived_html)
    assert 'class="admin-action-btn admin-action-btn--restore"' in restore_button
    assert "Restore contact" in restore_button


@pytest.mark.unit
def test_contact_edit_page_renders_themed_archive_and_restore_buttons() -> None:
    active_html = admin_contacts.render_contact_form_page(
        csrf_token="csrf",
        admin_username="operator",
        companies=[{"id": COMPANY_ID, "name": "Acme"}],
        contact={"id": CONTACT_ID, "full_name": "Ada", "company_id": COMPANY_ID},
    )
    active_button = _archive_form_button(active_html)
    assert 'class="admin-action-btn admin-action-btn--destructive"' in active_button
    assert "Archive contact" in active_button
    assert "admin-exit" not in active_button

    archived_html = admin_contacts.render_contact_form_page(
        csrf_token="csrf",
        admin_username="operator",
        companies=[],
        contact={"id": CONTACT_ID, "full_name": "Ada", "archived_at": "2026-01-01"},
    )
    restore_button = _archive_form_button(archived_html)
    assert 'class="admin-action-btn admin-action-btn--restore"' in restore_button
    assert "Restore contact" in restore_button


@pytest.mark.unit
def test_admin_css_asset_served_with_action_button_rules() -> None:
    response = client.get("/assets/admin.css")
    assert response.status_code == 200
    body = response.text
    assert ".admin-action-btn" in body
    assert ".admin-action-btn--destructive" in body
    assert ".admin-action-btn--restore" in body
    assert "appearance: none" in body
