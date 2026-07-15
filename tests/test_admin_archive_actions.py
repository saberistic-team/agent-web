"""Tests for themed archive/restore admin action buttons (#233)."""

from __future__ import annotations

import random
import re
from pathlib import Path
from uuid import UUID

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app import admin_contacts, admin_research_pages
from app.admin_auth import SESSION_COOKIE_NAME
from app.admin_layout import archive_restore_button_class, render_archive_restore_form
from app.admin_preview import (
    PREVIEW_COMPANY_ARCHIVE_ID,
    PREVIEW_COMPANY_RESTORE_ID,
    PREVIEW_CONTACT_ARCHIVE_ID,
    PREVIEW_CONTACT_RESTORE_ID,
    build_preview_company_research,
    build_preview_contact_edit,
    build_preview_contact_research,
)
from app.main import app

ADMIN_CSS = Path(__file__).resolve().parents[1] / "site/assets/admin.css"

client = TestClient(app, follow_redirects=False)

COMPANY_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
CONTACT_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "test-session-secret-32chars-minimum"


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


def _archive_button_markup(html: str) -> str | None:
    match = re.search(
        r'<button class="admin-action[^"]*" type="submit">[^<]+</button>',
        html,
    )
    return match.group(0) if match else None


@pytest.mark.unit
def test_archive_restore_button_class_variants() -> None:
    assert archive_restore_button_class(archived=False) == (
        "admin-action admin-action--destructive"
    )
    assert archive_restore_button_class(archived=True) == (
        "admin-action admin-action--secondary"
    )


@pytest.mark.unit
def test_render_archive_restore_form_emits_semantic_classes() -> None:
    archive_html = render_archive_restore_form(
        post_url="/admin/companies/1/archive",
        csrf_token="csrf",
        label="Archive company",
        archived=False,
    )
    assert 'class="admin-action admin-action--destructive"' in archive_html
    assert "Archive company" in archive_html
    assert 'name="csrf_token"' in archive_html

    restore_html = render_archive_restore_form(
        post_url="/admin/companies/1/restore",
        csrf_token="csrf",
        label="Restore company",
        archived=True,
    )
    assert 'class="admin-action admin-action--secondary"' in restore_html
    assert "Restore company" in restore_html


@pytest.mark.unit
def test_company_research_page_archive_button_uses_destructive_action() -> None:
    html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme"},
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    button = _archive_button_markup(html)
    assert button is not None
    assert 'admin-action--destructive' in button
    assert "Archive company" in button
    assert "admin-exit" not in button


@pytest.mark.unit
def test_company_research_page_restore_button_uses_secondary_action() -> None:
    html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme", "archived_at": "2026-01-01"},
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    button = _archive_button_markup(html)
    assert button is not None
    assert 'admin-action--secondary' in button
    assert "Restore company" in button


@pytest.mark.unit
def test_contact_research_and_edit_pages_use_action_classes() -> None:
    research_archive = admin_research_pages.render_admin_contact_research_page(
        contact={"id": CONTACT_ID, "full_name": "Ada", "buying_roles": []},
        company=None,
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert 'admin-action--destructive' in research_archive
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
    assert 'admin-action--secondary' in research_restore
    assert "Restore contact" in research_restore

    edit_archive = admin_contacts.render_contact_form_page(
        admin_username="operator",
        csrf_token="csrf",
        companies=[],
        contact={"id": CONTACT_ID, "full_name": "Ada"},
    )
    assert 'admin-action--destructive' in edit_archive

    edit_restore = admin_contacts.render_contact_form_page(
        admin_username="operator",
        csrf_token="csrf",
        companies=[],
        contact={"id": CONTACT_ID, "full_name": "Ada", "archived_at": "2026-01-01"},
    )
    assert 'admin-action--secondary' in edit_restore


@pytest.mark.unit
def test_admin_action_css_resets_native_button_appearance() -> None:
    css = ADMIN_CSS.read_text(encoding="utf-8")
    base = _rule_block(css, ".admin-action {")
    assert "font-family: inherit" in base
    assert "background:" in base
    assert "border:" in base
    assert "padding:" in base
    assert "cursor: pointer" in base
    assert "border-radius:" in base
    assert "color:" in base
    assert "background: none" not in base
    assert "background: white" not in base.lower()
    assert "background: #fff" not in base.lower()


@pytest.mark.unit
def test_admin_action_css_includes_interaction_and_disabled_states() -> None:
    css = ADMIN_CSS.read_text(encoding="utf-8")
    assert ".admin-action:focus-visible" in css
    assert ".admin-action:active:not(:disabled)" in css
    assert ".admin-action:disabled" in css
    assert ".admin-action--secondary:hover:not(:disabled)" in css
    assert ".admin-action--destructive:hover:not(:disabled)" in css
    assert ".admin-action--destructive:focus-visible:not(:disabled)" in css
    destructive = _rule_block(css, ".admin-action--destructive {")
    secondary = _rule_block(css, ".admin-action--secondary {")
    assert "background:" in destructive
    assert "border-color:" in destructive
    assert "color:" in destructive
    assert "background:" in secondary
    assert destructive != secondary


@pytest.mark.unit
def test_admin_exit_top_bar_styling_remains_separate_from_actions() -> None:
    css = ADMIN_CSS.read_text(encoding="utf-8")
    exit_block = _rule_block(css, ".admin-exit {")
    action_block = _rule_block(css, ".admin-action {")
    assert "text-decoration: none" in exit_block
    assert "border-bottom:" in exit_block
    assert "padding: 0.5rem" in action_block
    assert "border-radius:" in action_block
    assert "text-transform: uppercase" in action_block
    assert "border-bottom:" not in action_block


@pytest.mark.unit
def test_preview_company_and_contact_detail_builders_stable_with_seed() -> None:
    a = build_preview_company_research(PREVIEW_COMPANY_ARCHIVE_ID, rng=random.Random(42))
    b = build_preview_company_research(PREVIEW_COMPANY_ARCHIVE_ID, rng=random.Random(42))
    assert a is not None and b is not None
    assert a[0]["name"] == b[0]["name"]
    assert a[0]["archived_at"] is None

    restore = build_preview_company_research(
        PREVIEW_COMPANY_RESTORE_ID, rng=random.Random(42)
    )
    assert restore is not None
    assert restore[0]["archived_at"] is not None

    contact_a = build_preview_contact_research(
        PREVIEW_CONTACT_ARCHIVE_ID, rng=random.Random(7)
    )
    contact_b = build_preview_contact_research(
        PREVIEW_CONTACT_ARCHIVE_ID, rng=random.Random(7)
    )
    assert contact_a is not None and contact_b is not None
    assert contact_a[0]["full_name"] == contact_b[0]["full_name"]

    edit = build_preview_contact_edit(PREVIEW_CONTACT_RESTORE_ID, rng=random.Random(7))
    assert edit is not None
    assert edit[0]["archived_at"] is not None


@pytest.mark.unit
@pytest.mark.integration
def test_preview_routes_render_archive_and_restore_buttons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "42")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    routes = (
        (f"/admin/companies/{PREVIEW_COMPANY_ARCHIVE_ID}", "Archive company", "destructive"),
        (f"/admin/companies/{PREVIEW_COMPANY_RESTORE_ID}", "Restore company", "secondary"),
        (f"/admin/contacts/{PREVIEW_CONTACT_ARCHIVE_ID}", "Archive contact", "destructive"),
        (f"/admin/contacts/{PREVIEW_CONTACT_RESTORE_ID}", "Restore contact", "secondary"),
        (
            f"/admin/contacts/{PREVIEW_CONTACT_RESTORE_ID}/edit",
            "Restore contact",
            "secondary",
        ),
    )
    for path, label, variant in routes:
        response = client.get(
            path,
            cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
        )
        assert response.status_code == 200, path
        assert label in response.text
        assert f"admin-action--{variant}" in response.text
        assert 'class="admin-exit" type="submit"' not in response.text
