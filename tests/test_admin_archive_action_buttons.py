"""Regression tests for themed archive/restore admin action buttons (#233)."""

from __future__ import annotations

import re
from pathlib import Path
from uuid import UUID

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app import admin_contacts, admin_research_pages
from app.admin_auth import SESSION_COOKIE_NAME
from app.admin_layout import archive_restore_button_classes
from app.admin_preview import (
    PREVIEW_COMPANY_ARCHIVE_ID,
    PREVIEW_COMPANY_RESTORE_ID,
    PREVIEW_CONTACT_ARCHIVE_ID,
    PREVIEW_CONTACT_RESTORE_ID,
    build_preview_company_detail,
    build_preview_contact_detail,
)
from app.main import app

ADMIN_CSS = Path(__file__).resolve().parents[1] / "site/assets/admin.css"

COMPANY_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
CONTACT_ID = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")

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


def _archive_button_markup(html: str, label: str) -> str | None:
    pattern = (
        rf'<button class="admin-action admin-action--(?:destructive|restore)" '
        rf'type="submit">{re.escape(label)}</button>'
    )
    match = re.search(pattern, html)
    return match.group(0) if match else None


@pytest.mark.unit
def test_archive_restore_button_classes_map_to_semantic_variants() -> None:
    assert archive_restore_button_classes(archived=False) == (
        "admin-action admin-action--destructive"
    )
    assert archive_restore_button_classes(archived=True) == "admin-action admin-action--restore"


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


@pytest.mark.unit
def test_admin_action_variants_define_interactive_and_disabled_states() -> None:
    css = _admin_css()
    base_disabled = _rule_block(css, ".admin-action:disabled {")
    assert "cursor: not-allowed" in base_disabled
    for variant in ("destructive", "restore"):
        base = _rule_block(css, f".admin-action--{variant} {{")
        assert "border-color:" in base
        assert "background:" in base
        assert "color:" in base
        hover = _rule_block(css, f".admin-action--{variant}:hover {{")
        assert "border-color:" in hover
        focus = _rule_block(css, f".admin-action--{variant}:focus-visible {{")
        assert "outline" in focus
        disabled = _rule_block(css, f".admin-action--{variant}:disabled {{")
        assert "color: var(--muted)" in disabled


@pytest.mark.unit
def test_admin_action_not_applied_to_top_bar_exit_controls() -> None:
    css = _admin_css()
    exit_block = _rule_block(css, ".admin-exit {")
    assert "background:" not in exit_block
    signout_block = _rule_block(css, ".admin-signout {")
    assert "background: none" in signout_block


@pytest.mark.unit
def test_company_research_page_renders_destructive_archive_action() -> None:
    html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme"},
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert _archive_button_markup(html, "Archive company")
    assert 'class="admin-action admin-action--destructive"' in html


@pytest.mark.unit
def test_company_research_page_renders_restore_action_for_archived_company() -> None:
    html = admin_research_pages.render_admin_company_research_page(
        company={
            "id": PREVIEW_COMPANY_RESTORE_ID,
            "name": "Archived Northwind",
            "archived_at": "2026-06-01",
        },
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert _archive_button_markup(html, "Restore company")
    assert 'class="admin-action admin-action--restore"' in html


@pytest.mark.unit
def test_contact_research_and_edit_pages_render_themed_archive_actions() -> None:
    detail_html = admin_research_pages.render_admin_contact_research_page(
        contact={"id": CONTACT_ID, "full_name": "Ada", "buying_roles": []},
        company=None,
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert _archive_button_markup(detail_html, "Archive contact")

    edit_html = admin_contacts.render_contact_form_page(
        csrf_token="csrf",
        admin_username="operator",
        companies=[],
        contact={"id": CONTACT_ID, "full_name": "Ada"},
    )
    assert _archive_button_markup(edit_html, "Archive contact")

    archived_edit = admin_contacts.render_contact_form_page(
        csrf_token="csrf",
        admin_username="operator",
        companies=[],
        contact={"id": CONTACT_ID, "full_name": "Ada", "archived_at": "2026-01-01"},
    )
    assert 'class="admin-action admin-action--restore"' in archived_edit
    assert _archive_button_markup(archived_edit, "Restore contact")


@pytest.mark.unit
def test_admin_css_asset_served_with_action_button_rules() -> None:
    response = client.get("/assets/admin.css")
    assert response.status_code == 200
    body = response.text
    assert ".admin-action {" in body
    assert ".admin-action--destructive {" in body
    assert ".admin-action--restore {" in body


@pytest.mark.unit
@pytest.mark.parametrize(
    ("route", "label", "variant"),
    [
        (
            f"/admin/companies/{PREVIEW_COMPANY_ARCHIVE_ID}",
            "Archive company",
            "destructive",
        ),
        (
            f"/admin/companies/{PREVIEW_COMPANY_RESTORE_ID}",
            "Restore company",
            "restore",
        ),
        (
            f"/admin/contacts/{PREVIEW_CONTACT_ARCHIVE_ID}",
            "Archive contact",
            "destructive",
        ),
        (
            f"/admin/contacts/{PREVIEW_CONTACT_RESTORE_ID}",
            "Restore contact",
            "restore",
        ),
        (
            f"/admin/contacts/{PREVIEW_CONTACT_ARCHIVE_ID}/edit",
            "Archive contact",
            "destructive",
        ),
        (
            f"/admin/contacts/{PREVIEW_CONTACT_RESTORE_ID}/edit",
            "Restore contact",
            "restore",
        ),
    ],
)
def test_preview_routes_render_themed_archive_restore_buttons(
    monkeypatch: pytest.MonkeyPatch,
    route: str,
    label: str,
    variant: str,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "42")
    monkeypatch.setenv("ADMIN_USERNAME", "preview-admin")
    monkeypatch.setenv(
        "ADMIN_PASSWORD_HASH",
        PasswordHasher().hash("preview"),
    )
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "preview-session-secret-32chars-minimum")
    monkeypatch.setenv("BASE_URL", "http://127.0.0.1:8765")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    response = client.get(
        route,
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert response.status_code == 200
    assert f'class="admin-action admin-action--{variant}"' in response.text
    assert _archive_button_markup(response.text, label)


@pytest.mark.unit
def test_preview_company_and_contact_detail_builders_are_seed_stable() -> None:
    import random
    from datetime import datetime, timezone

    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    rng = random.Random(42)
    company_a = build_preview_company_detail(PREVIEW_COMPANY_ARCHIVE_ID, rng=rng, now=now)
    company_b = build_preview_company_detail(PREVIEW_COMPANY_ARCHIVE_ID, rng=rng, now=now)
    contact_a = build_preview_contact_detail(
        PREVIEW_CONTACT_RESTORE_ID, rng=random.Random(42), now=now
    )
    contact_b = build_preview_contact_detail(
        PREVIEW_CONTACT_RESTORE_ID, rng=random.Random(42), now=now
    )
    assert company_a == company_b
    assert contact_a == contact_b
    assert company_a is not None and company_a.get("archived_at") is None
    assert contact_a is not None and contact_a.get("archived_at") is not None
