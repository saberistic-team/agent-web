"""Regression tests for themed archive/restore admin action buttons (#233)."""

from __future__ import annotations

import re
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


def _archive_button_class_in_html(html: str) -> str | None:
    match = re.search(
        r'<button class="([^"]*)" type="submit">(?:Archive|Restore) (?:company|contact)</button>',
        html,
    )
    return match.group(1) if match else None


@pytest.mark.unit
def test_archive_action_button_class_maps_archive_and_restore() -> None:
    assert archive_action_button_class(is_archived=False) == (
        "admin-action admin-action--destructive"
    )
    assert archive_action_button_class(is_archived=True) == (
        "admin-action admin-action--secondary"
    )


@pytest.mark.unit
def test_admin_action_resets_native_button_appearance() -> None:
    css = _admin_css()
    block = _rule_block(css, ".admin-action {")
    assert "background:" in block
    assert "border:" in block
    assert "padding:" in block
    assert "font-family: inherit" in block
    assert "color:" in block
    assert "cursor: pointer" in block
    assert "border-radius:" in block
    assert "background: color-mix(in srgb, var(--surface)" in block
    assert "background: #fff" not in block
    assert "background: white" not in block


@pytest.mark.unit
def test_admin_action_destructive_and_secondary_states() -> None:
    css = _admin_css()
    destructive = _rule_block(css, ".admin-action--destructive {")
    assert "#e88a6a" in destructive
    assert "color: #ffb4b4" in destructive

    secondary_hover = _rule_block(css, ".admin-action--secondary:hover,")
    assert "border-color: var(--accent)" in secondary_hover
    assert "outline: 2px solid var(--accent)" in secondary_hover

    destructive_hover = _rule_block(css, ".admin-action--destructive:hover,")
    assert "outline: 2px solid #e88a6a" in destructive_hover

    disabled = _rule_block(css, ".admin-action:disabled,")
    assert "opacity: 0.5" in disabled
    assert "cursor: not-allowed" in disabled


@pytest.mark.unit
def test_company_research_page_archive_and_restore_markup() -> None:
    archive_html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme"},
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert _archive_button_class_in_html(archive_html) == (
        "admin-action admin-action--destructive"
    )
    assert "Archive company" in archive_html
    assert 'class="admin-exit" type="submit">Archive company' not in archive_html

    restore_html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme", "archived_at": "2026-07-01"},
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert _archive_button_class_in_html(restore_html) == (
        "admin-action admin-action--secondary"
    )
    assert "Restore company" in restore_html


@pytest.mark.unit
def test_contact_research_and_edit_pages_archive_and_restore_markup() -> None:
    contact = {
        "id": CONTACT_ID,
        "full_name": "Pat Example",
        "buying_roles": [],
    }
    research_archive = admin_research_pages.render_admin_contact_research_page(
        contact=contact,
        company=None,
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert _archive_button_class_in_html(research_archive) == (
        "admin-action admin-action--destructive"
    )

    research_restore = admin_research_pages.render_admin_contact_research_page(
        contact={**contact, "archived_at": "2026-07-01"},
        company=None,
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert _archive_button_class_in_html(research_restore) == (
        "admin-action admin-action--secondary"
    )

    edit_archive = admin_contacts.render_contact_form_page(
        csrf_token="csrf",
        admin_username="operator",
        companies=[],
        contact=contact,
    )
    assert _archive_button_class_in_html(edit_archive) == (
        "admin-action admin-action--destructive"
    )

    edit_restore = admin_contacts.render_contact_form_page(
        csrf_token="csrf",
        admin_username="operator",
        companies=[],
        contact={**contact, "archived_at": "2026-07-01"},
    )
    assert _archive_button_class_in_html(edit_restore) == (
        "admin-action admin-action--secondary"
    )


@pytest.mark.unit
@pytest.mark.integration
def test_preview_company_detail_pages_render_archive_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "233")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    archive_response = client.get(
        f"/admin/companies/{PREVIEW_COMPANY_ACTIVE_ID}",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert archive_response.status_code == 200
    assert 'class="admin-action admin-action--destructive"' in archive_response.text
    assert "Archive company" in archive_response.text

    restore_response = client.get(
        f"/admin/companies/{PREVIEW_COMPANY_ARCHIVED_ID}",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert restore_response.status_code == 200
    assert 'class="admin-action admin-action--secondary"' in restore_response.text
    assert "Restore company" in restore_response.text


@pytest.mark.unit
@pytest.mark.integration
def test_preview_contact_detail_and_edit_pages_render_archive_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "233")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    for path, destructive_label, destructive_class in (
        (f"/admin/contacts/{PREVIEW_CONTACT_ACTIVE_ID}", "Archive contact", True),
        (f"/admin/contacts/{PREVIEW_CONTACT_ARCHIVED_ID}", "Restore contact", False),
        (f"/admin/contacts/{PREVIEW_CONTACT_ACTIVE_ID}/edit", "Archive contact", True),
        (f"/admin/contacts/{PREVIEW_CONTACT_ARCHIVED_ID}/edit", "Restore contact", False),
    ):
        response = client.get(
            path,
            cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
        )
        assert response.status_code == 200
        assert destructive_label in response.text
        expected_class = (
            "admin-action admin-action--destructive"
            if destructive_class
            else "admin-action admin-action--secondary"
        )
        assert f'class="{expected_class}"' in response.text
