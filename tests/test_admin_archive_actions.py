"""Regression tests for themed Archive/Restore admin action buttons (#233)."""

from __future__ import annotations

import random
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app import admin_contacts, admin_research_pages
from app.admin_auth import SESSION_COOKIE_NAME
from app.admin_layout import admin_archive_button_class, render_admin_archive_form
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
def test_admin_archive_button_class_maps_archive_and_restore() -> None:
    assert admin_archive_button_class(archived=False) == (
        "admin-action admin-action--destructive"
    )
    assert admin_archive_button_class(archived=True) == "admin-action admin-action--secondary"


@pytest.mark.unit
def test_render_admin_archive_form_uses_semantic_classes() -> None:
    archive_html = render_admin_archive_form(
        entity_path="/admin/companies/1",
        entity_label="company",
        csrf_token="csrf",
        archived_at=None,
    )
    assert 'class="admin-action admin-action--destructive"' in archive_html
    assert "Archive company" in archive_html
    assert 'action="/admin/companies/1/archive"' in archive_html

    restore_html = render_admin_archive_form(
        entity_path="/admin/contacts/2",
        entity_label="contact",
        csrf_token="csrf",
        archived_at="2026-01-01",
    )
    assert 'class="admin-action admin-action--secondary"' in restore_html
    assert "Restore contact" in restore_html
    assert 'action="/admin/contacts/2/restore"' in restore_html
    assert '<button class="admin-exit"' not in restore_html


@pytest.mark.unit
def test_company_research_page_archive_and_restore_markup() -> None:
    archive_html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme"},
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert 'class="admin-action admin-action--destructive"' in archive_html
    assert "Archive company" in archive_html
    assert '<button class="admin-exit"' not in archive_html

    restore_html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme", "archived_at": "2026-01-01"},
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert 'class="admin-action admin-action--secondary"' in restore_html
    assert "Restore company" in restore_html


@pytest.mark.unit
def test_contact_research_and_edit_pages_use_archive_action_classes() -> None:
    detail_html = admin_research_pages.render_admin_contact_research_page(
        contact={"id": CONTACT_ID, "full_name": "Ada", "buying_roles": []},
        company=None,
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert 'class="admin-action admin-action--destructive"' in detail_html
    assert "Archive contact" in detail_html

    edit_html = admin_contacts.render_contact_form_page(
        admin_username="operator",
        csrf_token="csrf",
        companies=[],
        contact={"id": CONTACT_ID, "full_name": "Ada", "archived_at": "2026-01-01"},
    )
    assert 'class="admin-action admin-action--secondary"' in edit_html
    assert "Restore contact" in edit_html


@pytest.mark.unit
def test_admin_action_css_resets_native_button_appearance() -> None:
    css = _admin_css()
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
    assert "#ffb4b4" in destructive

    secondary = _rule_block(css, ".admin-action--secondary {")
    assert "background:" in secondary
    assert "var(--ink)" in secondary


@pytest.mark.unit
def test_admin_action_css_includes_interaction_and_disabled_states() -> None:
    css = _admin_css()
    assert ".admin-action:hover" in css
    assert ".admin-action:focus-visible" in css
    assert ".admin-action:active:not(:disabled)" in css
    assert ".admin-action:disabled" in css
    assert "cursor: not-allowed" in _rule_block(css, ".admin-action:disabled")
    assert "opacity:" in _rule_block(css, ".admin-action:disabled")
    assert ".admin-action--destructive:hover:not(:disabled)" in css
    assert ".admin-action--secondary:hover:not(:disabled)" in css
    assert "outline:" in _rule_block(css, ".admin-action:focus-visible")


@pytest.mark.unit
def test_admin_action_css_is_served_with_admin_shell() -> None:
    response = client.get("/assets/admin.css")
    assert response.status_code == 200
    body = response.text
    assert ".admin-action {" in body
    assert ".admin-action--destructive" in body
    assert ".admin-action--secondary" in body


@pytest.mark.unit
@pytest.mark.parametrize(
    ("route", "expected_class", "expected_label"),
    (
        (
            f"/admin/companies/{PREVIEW_COMPANY_ARCHIVE_ID}",
            "admin-action--destructive",
            "Archive company",
        ),
        (
            f"/admin/companies/{PREVIEW_COMPANY_RESTORE_ID}",
            "admin-action--secondary",
            "Restore company",
        ),
        (
            f"/admin/contacts/{PREVIEW_CONTACT_ARCHIVE_ID}",
            "admin-action--destructive",
            "Archive contact",
        ),
        (
            f"/admin/contacts/{PREVIEW_CONTACT_RESTORE_ID}",
            "admin-action--secondary",
            "Restore contact",
        ),
        (
            f"/admin/contacts/{PREVIEW_CONTACT_ARCHIVE_ID}/edit",
            "admin-action--destructive",
            "Archive contact",
        ),
        (
            f"/admin/contacts/{PREVIEW_CONTACT_RESTORE_ID}/edit",
            "admin-action--secondary",
            "Restore contact",
        ),
    ),
)
def test_preview_detail_and_edit_pages_render_archive_actions(
    monkeypatch: pytest.MonkeyPatch,
    route: str,
    expected_class: str,
    expected_label: str,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "42")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    response = client.get(
        route,
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert response.status_code == 200
    assert expected_class in response.text
    assert expected_label in response.text
    assert f'class="admin-action {expected_class}"' in response.text
    assert f'<button class="admin-exit"' not in response.text
    assert "No research records yet" not in response.text


@pytest.mark.unit
def test_preview_company_and_contact_detail_seed_stable() -> None:
    company_a = preview_company_detail(PREVIEW_COMPANY_ARCHIVE_ID, rng=random.Random(42))
    company_b = preview_company_detail(PREVIEW_COMPANY_ARCHIVE_ID, rng=random.Random(42))
    assert company_a == company_b
    assert company_a is not None and company_a.get("archived_at") is None

    archived = preview_company_detail(PREVIEW_COMPANY_RESTORE_ID, rng=random.Random(42))
    assert archived is not None and archived.get("archived_at") is not None

    contact_a = preview_contact_detail(PREVIEW_CONTACT_ARCHIVE_ID, rng=random.Random(7))
    contact_b = preview_contact_detail(PREVIEW_CONTACT_ARCHIVE_ID, rng=random.Random(7))
    assert contact_a == contact_b
