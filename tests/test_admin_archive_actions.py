"""Regression tests for themed archive/restore admin action buttons (#233)."""

from __future__ import annotations

import random
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app import admin_contacts, admin_research_pages
from app.admin_auth import SESSION_COOKIE_NAME
from app.admin_layout import archive_restore_action_class
from app.admin_preview import (
    PREVIEW_COMPANY_ARCHIVE_ID,
    PREVIEW_COMPANY_RESTORE_ID,
    PREVIEW_CONTACT_ARCHIVE_ID,
    PREVIEW_CONTACT_RESTORE_ID,
    build_preview_company_research,
    build_preview_contact_research,
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
def test_archive_restore_action_class_maps_to_semantic_variants() -> None:
    assert archive_restore_action_class(is_archived=False) == (
        "admin-action admin-action--destructive"
    )
    assert archive_restore_action_class(is_archived=True) == (
        "admin-action admin-action--secondary"
    )


@pytest.mark.unit
def test_admin_action_css_resets_native_button_appearance() -> None:
    css = _admin_css()
    base = _rule_block(css, ".admin-action {")
    assert "appearance: none" in base
    assert "-webkit-appearance: none" in base
    assert "background:" not in base
    assert "border: 1px solid transparent" in base
    assert "padding: 0.5rem 0.85rem" in base
    assert "cursor: pointer" in base
    assert "border-radius: 2px" in base
    assert "font-family: inherit" in base

    destructive = _rule_block(css, ".admin-action--destructive {")
    assert "background:" in destructive
    assert "border-color:" in destructive
    assert "color:" in destructive
    assert "#e05a5a" in destructive or "var(--surface)" in destructive

    secondary = _rule_block(css, ".admin-action--secondary {")
    assert "background:" in secondary
    assert "border-color: var(--line)" in secondary
    assert "color: var(--ink)" in secondary


@pytest.mark.unit
def test_admin_action_css_includes_interaction_and_disabled_states() -> None:
    css = _admin_css()
    for variant in ("destructive", "secondary"):
        assert f".admin-action--{variant}:hover" in css
        assert f".admin-action--{variant}:focus-visible" in css
        assert f".admin-action--{variant}:active" in css
        assert f".admin-action--{variant}:disabled" in css
        disabled = _rule_block(css, f".admin-action--{variant}:disabled")
        assert "cursor: not-allowed" in disabled
        assert "opacity:" in disabled


@pytest.mark.unit
def test_admin_exit_remains_separate_from_form_action_buttons() -> None:
    css = _admin_css()
    exit_block = _rule_block(css, ".admin-exit {")
    assert "border-bottom: 1px solid transparent" in exit_block
    assert "padding:" not in exit_block
    assert ".admin-exit" not in _rule_block(css, ".admin-action--destructive {")


@pytest.mark.unit
def test_company_research_page_renders_themed_archive_button() -> None:
    html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme"},
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert 'class="admin-action admin-action--destructive" type="submit">Archive company' in html
    assert 'class="admin-exit" type="submit">Archive company' not in html


@pytest.mark.unit
def test_company_research_page_renders_themed_restore_button() -> None:
    html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme", "archived_at": "2026-01-01"},
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert 'class="admin-action admin-action--secondary" type="submit">Restore company' in html


@pytest.mark.unit
def test_contact_research_page_renders_themed_archive_and_restore_buttons() -> None:
    active_html = admin_research_pages.render_admin_contact_research_page(
        contact={"id": CONTACT_ID, "full_name": "Pat", "buying_roles": []},
        company=None,
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert 'class="admin-action admin-action--destructive" type="submit">Archive contact' in active_html

    archived_html = admin_research_pages.render_admin_contact_research_page(
        contact={
            "id": CONTACT_ID,
            "full_name": "Pat",
            "buying_roles": [],
            "archived_at": "2026-01-01",
        },
        company=None,
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert (
        'class="admin-action admin-action--secondary" type="submit">Restore contact'
        in archived_html
    )


@pytest.mark.unit
def test_contact_edit_page_renders_themed_archive_and_restore_buttons() -> None:
    archive_html = admin_contacts.render_contact_form_page(
        csrf_token="csrf",
        admin_username="operator",
        companies=[],
        contact={"id": CONTACT_ID, "full_name": "Pat"},
    )
    assert 'class="admin-action admin-action--destructive" type="submit">Archive contact' in archive_html

    restore_html = admin_contacts.render_contact_form_page(
        csrf_token="csrf",
        admin_username="operator",
        companies=[],
        contact={"id": CONTACT_ID, "full_name": "Pat", "archived_at": "2026-01-01"},
    )
    assert 'class="admin-action admin-action--secondary" type="submit">Restore contact' in restore_html


@pytest.mark.unit
def test_preview_company_and_contact_detail_seed_stable() -> None:
    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    rng = random.Random(42)
    company_archive = build_preview_company_research(
        PREVIEW_COMPANY_ARCHIVE_ID, rng=rng, now=now
    )
    company_restore = build_preview_company_research(
        PREVIEW_COMPANY_RESTORE_ID, rng=rng, now=now
    )
    contact_archive = build_preview_contact_research(
        PREVIEW_CONTACT_ARCHIVE_ID, rng=rng, now=now
    )
    contact_restore = build_preview_contact_research(
        PREVIEW_CONTACT_RESTORE_ID, rng=rng, now=now
    )
    assert company_archive is not None
    assert company_restore is not None
    assert contact_archive is not None
    assert contact_restore is not None
    assert company_archive[0].get("archived_at") is None
    assert company_restore[0].get("archived_at") is not None
    assert contact_archive[0].get("archived_at") is None
    assert contact_restore[0].get("archived_at") is not None


@pytest.mark.unit
@pytest.mark.integration
@pytest.mark.parametrize(
    ("path", "expected_class", "label"),
    [
        (
            f"/admin/companies/{PREVIEW_COMPANY_ARCHIVE_ID}",
            "admin-action admin-action--destructive",
            "Archive company",
        ),
        (
            f"/admin/companies/{PREVIEW_COMPANY_RESTORE_ID}",
            "admin-action admin-action--secondary",
            "Restore company",
        ),
        (
            f"/admin/contacts/{PREVIEW_CONTACT_ARCHIVE_ID}",
            "admin-action admin-action--destructive",
            "Archive contact",
        ),
        (
            f"/admin/contacts/{PREVIEW_CONTACT_RESTORE_ID}",
            "admin-action admin-action--secondary",
            "Restore contact",
        ),
        (
            f"/admin/contacts/{PREVIEW_CONTACT_ARCHIVE_ID}/edit",
            "admin-action admin-action--destructive",
            "Archive contact",
        ),
        (
            f"/admin/contacts/{PREVIEW_CONTACT_RESTORE_ID}/edit",
            "admin-action admin-action--secondary",
            "Restore contact",
        ),
    ],
)
def test_preview_detail_pages_render_themed_archive_restore_buttons(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    expected_class: str,
    label: str,
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
    assert f'class="{expected_class}" type="submit">{label}' in response.text
    assert 'class="admin-exit" type="submit"' not in response.text
