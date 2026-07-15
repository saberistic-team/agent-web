"""Regression tests for themed Archive/Restore admin action buttons (#233)."""

from __future__ import annotations

import re
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app import admin_contacts, admin_research_pages
from app.admin_auth import SESSION_COOKIE_NAME
from app.admin_layout import render_archive_action_button
from app.admin_preview import (
    PREVIEW_COMPANY_ARCHIVE_ID,
    PREVIEW_COMPANY_RESTORE_ID,
    PREVIEW_CONTACT_ARCHIVE_ID,
    PREVIEW_CONTACT_RESTORE_ID,
    build_preview_company_detail,
    build_preview_contact_detail,
    build_preview_contact_edit,
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


def _archive_button_markup(html: str) -> str:
    match = re.search(
        r'<form method="post" action="[^"]+/(?:archive|restore)">.*?'
        r'(<button class="admin-action[^"]*" type="submit">[^<]+</button>)',
        html,
        flags=re.DOTALL,
    )
    assert match is not None, "expected archive/restore form button"
    return match.group(1)


@pytest.mark.unit
def test_render_archive_action_button_archive_uses_destructive_class() -> None:
    button = render_archive_action_button(archived=False, entity="company")
    assert 'class="admin-action admin-action--destructive"' in button
    assert "Archive company" in button
    assert "admin-exit" not in button


@pytest.mark.unit
def test_render_archive_action_button_restore_uses_secondary_class() -> None:
    button = render_archive_action_button(archived=True, entity="contact")
    assert 'class="admin-action admin-action--secondary"' in button
    assert "Restore contact" in button
    assert "admin-exit" not in button


@pytest.mark.unit
def test_company_research_page_renders_themed_archive_button() -> None:
    html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme"},
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    button = _archive_button_markup(html)
    assert 'class="admin-action admin-action--destructive"' in button
    assert "Archive company" in button
    assert 'action="/admin/companies/' in html
    assert "/archive" in html


@pytest.mark.unit
def test_company_research_page_renders_themed_restore_button() -> None:
    html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme", "archived_at": "2026-01-01"},
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    button = _archive_button_markup(html)
    assert 'class="admin-action admin-action--secondary"' in button
    assert "Restore company" in button
    assert "/restore" in html


@pytest.mark.unit
def test_contact_research_page_renders_themed_archive_button() -> None:
    html = admin_research_pages.render_admin_contact_research_page(
        contact={"id": CONTACT_ID, "full_name": "Pat", "buying_roles": []},
        company=None,
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    button = _archive_button_markup(html)
    assert 'class="admin-action admin-action--destructive"' in button
    assert "Archive contact" in button


@pytest.mark.unit
def test_contact_form_page_renders_themed_restore_button() -> None:
    html = admin_contacts.render_contact_form_page(
        admin_username="operator",
        csrf_token="csrf",
        companies=[],
        contact={"id": CONTACT_ID, "full_name": "Pat", "archived_at": "2026-01-01"},
    )
    button = _archive_button_markup(html)
    assert 'class="admin-action admin-action--secondary"' in button
    assert "Restore contact" in button


@pytest.mark.unit
def test_admin_action_css_resets_native_button_appearance() -> None:
    css = _admin_css()
    base = _rule_block(css, ".admin-action {")
    assert "font-family: inherit" in base
    assert "padding: 0.5rem 0.85rem" in base
    assert "border-radius: 2px" in base
    assert "cursor: pointer" in base
    assert "border: 1px solid var(--line)" in base
    assert "color: var(--ink)" in base

    secondary = _rule_block(css, ".admin-action--secondary {")
    assert "background:" in secondary
    assert "var(--surface)" in secondary

    destructive = _rule_block(css, ".admin-action--destructive {")
    assert "background:" in destructive
    assert "#e05a5a" in destructive

    disabled = _rule_block(css, ".admin-action:disabled {")
    assert "opacity: 0.55" in disabled
    assert "cursor: not-allowed" in disabled

    focus = _rule_block(css, ".admin-action:focus-visible {")
    assert "outline: 2px solid var(--accent)" in focus

    hover_secondary = _rule_block(css, ".admin-action--secondary:hover:not(:disabled) {")
    assert "border-color: var(--accent)" in hover_secondary

    hover_destructive = _rule_block(css, ".admin-action--destructive:hover:not(:disabled) {")
    assert "border-color: #e88a6a" in hover_destructive


@pytest.mark.unit
def test_admin_action_css_does_not_use_browser_default_white_background() -> None:
    css = _admin_css()
    for modifier in ("--secondary", "--destructive"):
        block = _rule_block(css, f".admin-action{modifier} {{")
        assert "background: white" not in block
        assert "background: #fff" not in block
        assert "background: #ffffff" not in block
        assert "background:" in block


@pytest.mark.unit
def test_preview_company_detail_builders_are_seed_stable() -> None:
    import random

    a = build_preview_company_detail(PREVIEW_COMPANY_ARCHIVE_ID, rng=random.Random(42))
    b = build_preview_company_detail(PREVIEW_COMPANY_ARCHIVE_ID, rng=random.Random(42))
    assert a is not None and b is not None
    assert a[0]["name"] == b[0]["name"]
    assert a[0]["archived_at"] is None

    restored = build_preview_company_detail(PREVIEW_COMPANY_RESTORE_ID, rng=random.Random(42))
    assert restored is not None
    assert restored[0]["archived_at"] is not None


@pytest.mark.unit
def test_preview_contact_detail_and_edit_cover_archive_and_restore() -> None:
    import random

    active_detail = build_preview_contact_detail(
        PREVIEW_CONTACT_ARCHIVE_ID, rng=random.Random(7)
    )
    archived_detail = build_preview_contact_detail(
        PREVIEW_CONTACT_RESTORE_ID, rng=random.Random(7)
    )
    active_edit = build_preview_contact_edit(
        PREVIEW_CONTACT_ARCHIVE_ID, rng=random.Random(7)
    )
    archived_edit = build_preview_contact_edit(
        PREVIEW_CONTACT_RESTORE_ID, rng=random.Random(7)
    )
    assert active_detail is not None and archived_detail is not None
    assert active_edit is not None and archived_edit is not None
    assert active_detail[0]["archived_at"] is None
    assert archived_detail[0]["archived_at"] is not None
    assert active_edit[0]["archived_at"] is None
    assert archived_edit[0]["archived_at"] is not None


@pytest.mark.unit
@pytest.mark.parametrize(
    ("path", "expected_class", "expected_label"),
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
def test_preview_routes_render_themed_archive_buttons(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    expected_class: str,
    expected_label: str,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "42")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    response = client.get(
        path,
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert response.status_code == 200
    assert expected_class in response.text
    assert expected_label in response.text
    assert 'class="admin-exit" type="submit">Archive' not in response.text
    assert 'class="admin-exit" type="submit">Restore' not in response.text
