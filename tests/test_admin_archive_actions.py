"""Archive/restore admin action button styling (#233)."""

from __future__ import annotations

import re
from pathlib import Path
from uuid import UUID

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app import admin_contacts, admin_research_pages
from app.admin_auth import SESSION_COOKIE_NAME
from app.admin_preview import (
    PREVIEW_COMPANY_ARCHIVE_ACTION_ID,
    PREVIEW_COMPANY_RESTORE_ACTION_ID,
    PREVIEW_CONTACT_ARCHIVE_ACTION_ID,
    PREVIEW_CONTACT_RESTORE_ACTION_ID,
    preview_company_detail_state,
    preview_contact_detail_state,
    preview_contact_edit_state,
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


def _archive_button_markup(html: str, label: str) -> str | None:
    pattern = (
        rf'<button class="([^"]+)" type="submit">{re.escape(label)}</button>'
    )
    match = re.search(pattern, html)
    return match.group(1) if match else None


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


@pytest.mark.unit
def test_admin_action_css_includes_interaction_and_disabled_states() -> None:
    css = _admin_css()
    assert ".admin-action:focus-visible" in css
    assert ".admin-action:active:not(:disabled)" in css
    assert ".admin-action:disabled" in css
    assert ".admin-action--destructive:hover:not(:disabled)" in css
    assert ".admin-action--restore:hover:not(:disabled)" in css
    destructive_focus = _rule_block(css, ".admin-action--destructive:focus-visible")
    assert "outline-color:" in destructive_focus


@pytest.mark.unit
def test_company_research_archive_and_restore_use_semantic_action_classes() -> None:
    active_html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme"},
        contacts=[],
        records=[],
        csrf_token="csrf",
    )
    archived_html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme", "archived_at": "2026-01-01"},
        contacts=[],
        records=[],
        csrf_token="csrf",
    )

    archive_classes = _archive_button_markup(active_html, "Archive company")
    restore_classes = _archive_button_markup(archived_html, "Restore company")
    assert archive_classes == "admin-action admin-action--destructive"
    assert restore_classes == "admin-action admin-action--restore"
    assert 'class="admin-exit" type="submit">Archive company' not in active_html
    assert 'class="admin-exit" type="submit">Restore company' not in archived_html


@pytest.mark.unit
def test_contact_research_archive_and_restore_use_semantic_action_classes() -> None:
    active_html = admin_research_pages.render_admin_contact_research_page(
        contact={"id": CONTACT_ID, "full_name": "Pat", "buying_roles": []},
        company=None,
        records=[],
        csrf_token="csrf",
    )
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
    )

    archive_classes = _archive_button_markup(active_html, "Archive contact")
    restore_classes = _archive_button_markup(archived_html, "Restore contact")
    assert archive_classes == "admin-action admin-action--destructive"
    assert restore_classes == "admin-action admin-action--restore"


@pytest.mark.unit
def test_contact_edit_archive_and_restore_use_semantic_action_classes() -> None:
    active_html = admin_contacts.render_contact_form_page(
        admin_username="operator",
        csrf_token="csrf",
        companies=[],
        contact={"id": CONTACT_ID, "full_name": "Pat"},
    )
    archived_html = admin_contacts.render_contact_form_page(
        admin_username="operator",
        csrf_token="csrf",
        companies=[],
        contact={"id": CONTACT_ID, "full_name": "Pat", "archived_at": "2026-01-01"},
    )

    assert (
        _archive_button_markup(active_html, "Archive contact")
        == "admin-action admin-action--destructive"
    )
    assert (
        _archive_button_markup(archived_html, "Restore contact")
        == "admin-action admin-action--restore"
    )


@pytest.mark.unit
def test_preview_archive_restore_detail_states_are_stable(monkeypatch: pytest.MonkeyPatch) -> None:
    import random

    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "233")
    rng = random.Random(233)
    company, contacts, records = preview_company_detail_state(
        PREVIEW_COMPANY_ARCHIVE_ACTION_ID,
        rng=rng,
    )
    assert company["name"] == "Northwind Labs"
    assert "archived_at" not in company
    assert contacts
    assert records

    archived_company, _, _ = preview_company_detail_state(
        PREVIEW_COMPANY_RESTORE_ACTION_ID,
        rng=rng,
    )
    assert archived_company["archived_at"]

    contact, company_row, contact_records = preview_contact_detail_state(
        PREVIEW_CONTACT_ARCHIVE_ACTION_ID,
        rng=rng,
    )
    assert contact["full_name"]
    assert "archived_at" not in contact
    assert company_row["name"]
    assert contact_records

    archived_contact, _, _ = preview_contact_detail_state(
        PREVIEW_CONTACT_RESTORE_ACTION_ID,
        rng=rng,
    )
    assert archived_contact["archived_at"]

    edit_contact, companies = preview_contact_edit_state(
        PREVIEW_CONTACT_ARCHIVE_ACTION_ID,
        rng=rng,
    )
    assert edit_contact["id"] == str(PREVIEW_CONTACT_ARCHIVE_ACTION_ID)
    assert companies


@pytest.fixture
def preview_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "233")
    monkeypatch.setenv("ADMIN_USERNAME", "preview-admin")
    monkeypatch.setenv(
        "ADMIN_PASSWORD_HASH",
        PasswordHasher().hash("preview"),
    )
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "preview-session-secret-32chars-minimum")
    monkeypatch.setenv("BASE_URL", "http://127.0.0.1:8765")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    return TestClient(app, follow_redirects=False)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("route", "expected_label", "expected_modifier"),
    [
        (
            f"/admin/companies/{PREVIEW_COMPANY_ARCHIVE_ACTION_ID}",
            "Archive company",
            "admin-action--destructive",
        ),
        (
            f"/admin/companies/{PREVIEW_COMPANY_RESTORE_ACTION_ID}",
            "Restore company",
            "admin-action--restore",
        ),
        (
            f"/admin/contacts/{PREVIEW_CONTACT_ARCHIVE_ACTION_ID}",
            "Archive contact",
            "admin-action--destructive",
        ),
        (
            f"/admin/contacts/{PREVIEW_CONTACT_RESTORE_ACTION_ID}",
            "Restore contact",
            "admin-action--restore",
        ),
        (
            f"/admin/contacts/{PREVIEW_CONTACT_ARCHIVE_ACTION_ID}/edit",
            "Archive contact",
            "admin-action--destructive",
        ),
        (
            f"/admin/contacts/{PREVIEW_CONTACT_RESTORE_ACTION_ID}/edit",
            "Restore contact",
            "admin-action--restore",
        ),
    ],
)
def test_preview_archive_restore_pages_render_themed_action_buttons(
    preview_client: TestClient,
    route: str,
    expected_label: str,
    expected_modifier: str,
) -> None:
    response = preview_client.get(
        route,
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert response.status_code == 200
    classes = _archive_button_markup(response.text, expected_label)
    assert classes == f"admin-action {expected_modifier}"
    assert 'class="admin-exit" type="submit"' not in response.text
