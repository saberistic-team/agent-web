"""Regression tests for Archive/Restore admin action button styling (#233)."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app import admin_contacts, admin_research_pages
from app.admin_auth import SESSION_COOKIE_NAME
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


def _archive_button_markup(html: str, label: str) -> str:
    match = re.search(
        rf'<button class="([^"]+)" type="submit">{re.escape(label)}</button>',
        html,
    )
    assert match is not None, f"Missing {label!r} button"
    return match.group(1)


@pytest.mark.unit
def test_admin_action_css_resets_native_button_appearance() -> None:
    css = _admin_css()
    block = _rule_block(css, ".admin-action {")
    assert "background:" in block
    assert "border:" in block
    assert "padding:" in block
    assert "font-family: inherit" in block
    assert "color:" in block
    assert "cursor: pointer" in block
    assert "border-radius:" in block

    exit_block = _rule_block(css, ".admin-exit {")
    assert "background:" not in exit_block
    assert "padding:" not in exit_block


@pytest.mark.unit
def test_admin_action_css_includes_interactive_states() -> None:
    css = _admin_css()
    assert ".admin-action:hover:not(:disabled)" in css
    assert ".admin-action:focus-visible" in css
    assert ".admin-action:active:not(:disabled)" in css
    assert ".admin-action:disabled" in css
    assert ".admin-action--destructive" in css
    assert ".admin-action--secondary" in css

    disabled_block = _rule_block(css, ".admin-action:disabled {")
    assert "opacity:" in disabled_block
    assert "cursor: not-allowed" in disabled_block

    destructive_block = _rule_block(css, ".admin-action--destructive {")
    assert "background:" in destructive_block
    assert "#e88a6a" in destructive_block
    assert "background: #fff" not in destructive_block
    assert "background: white" not in destructive_block


@pytest.mark.unit
def test_company_research_renders_destructive_archive_button() -> None:
    html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme"},
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    classes = _archive_button_markup(html, "Archive company")
    assert classes == "admin-action admin-action--destructive"
    assert "admin-exit" not in classes


@pytest.mark.unit
def test_company_research_renders_secondary_restore_button() -> None:
    html = admin_research_pages.render_admin_company_research_page(
        company={
            "id": COMPANY_ID,
            "name": "Acme",
            "archived_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        },
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    classes = _archive_button_markup(html, "Restore company")
    assert classes == "admin-action admin-action--secondary"


@pytest.mark.unit
def test_contact_research_renders_archive_and_restore_classes() -> None:
    active_html = admin_research_pages.render_admin_contact_research_page(
        contact={"id": CONTACT_ID, "full_name": "Pat", "buying_roles": []},
        company={"id": COMPANY_ID, "name": "Acme"},
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert (
        _archive_button_markup(active_html, "Archive contact")
        == "admin-action admin-action--destructive"
    )

    archived_html = admin_research_pages.render_admin_contact_research_page(
        contact={
            "id": CONTACT_ID,
            "full_name": "Pat",
            "buying_roles": [],
            "archived_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        },
        company={"id": COMPANY_ID, "name": "Acme"},
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert (
        _archive_button_markup(archived_html, "Restore contact")
        == "admin-action admin-action--secondary"
    )


@pytest.mark.unit
def test_contact_edit_renders_archive_and_restore_classes() -> None:
    archive_html = admin_contacts.render_contact_form_page(
        csrf_token="csrf",
        admin_username="operator",
        companies=[{"id": COMPANY_ID, "name": "Acme"}],
        contact={"id": CONTACT_ID, "full_name": "Pat"},
    )
    assert (
        _archive_button_markup(archive_html, "Archive contact")
        == "admin-action admin-action--destructive"
    )

    restore_html = admin_contacts.render_contact_form_page(
        csrf_token="csrf",
        admin_username="operator",
        companies=[],
        contact={
            "id": CONTACT_ID,
            "full_name": "Pat",
            "archived_at": "2026-01-01",
        },
    )
    assert (
        _archive_button_markup(restore_html, "Restore contact")
        == "admin-action admin-action--secondary"
    )


@pytest.mark.unit
def test_admin_css_asset_served_with_action_button_rules() -> None:
    response = client.get("/assets/admin.css")
    assert response.status_code == 200
    body = response.text
    assert ".admin-action {" in body
    assert ".admin-action--destructive {" in body
    assert ".admin-action--secondary {" in body


@pytest.mark.unit
@pytest.mark.parametrize(
    ("path", "label", "expected_class"),
    [
        (
            f"/admin/companies/{PREVIEW_COMPANY_ACTIVE_ID}",
            "Archive company",
            "admin-action admin-action--destructive",
        ),
        (
            f"/admin/companies/{PREVIEW_COMPANY_ARCHIVED_ID}",
            "Restore company",
            "admin-action admin-action--secondary",
        ),
        (
            f"/admin/contacts/{PREVIEW_CONTACT_ACTIVE_ID}",
            "Archive contact",
            "admin-action admin-action--destructive",
        ),
        (
            f"/admin/contacts/{PREVIEW_CONTACT_ARCHIVED_ID}",
            "Restore contact",
            "admin-action admin-action--secondary",
        ),
        (
            f"/admin/contacts/{PREVIEW_CONTACT_ACTIVE_ID}/edit",
            "Archive contact",
            "admin-action admin-action--destructive",
        ),
        (
            f"/admin/contacts/{PREVIEW_CONTACT_ARCHIVED_ID}/edit",
            "Restore contact",
            "admin-action admin-action--secondary",
        ),
    ],
)
def test_preview_routes_render_themed_archive_restore_buttons(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    label: str,
    expected_class: str,
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
        path,
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert response.status_code == 200
    assert _archive_button_markup(response.text, label) == expected_class
    assert 'class="cta admin-submit"' in response.text


@pytest.mark.unit
def test_preview_company_and_contact_detail_builders_are_stable_with_seed() -> None:
    import random

    fixed_now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    company_a = build_preview_company_detail(
        PREVIEW_COMPANY_ACTIVE_ID,
        rng=random.Random(42),
        now=fixed_now,
    )
    company_b = build_preview_company_detail(
        PREVIEW_COMPANY_ACTIVE_ID,
        rng=random.Random(42),
        now=fixed_now,
    )
    contact_a = build_preview_contact_detail(
        PREVIEW_CONTACT_ARCHIVED_ID,
        rng=random.Random(42),
        now=fixed_now,
    )
    contact_b = build_preview_contact_detail(
        PREVIEW_CONTACT_ARCHIVED_ID,
        rng=random.Random(42),
        now=fixed_now,
    )
    assert company_a == company_b
    assert contact_a == contact_b
    assert company_a is not None and company_a.get("archived_at") is None
    assert contact_a is not None and contact_a.get("archived_at") is not None
