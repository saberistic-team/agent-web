"""Archive/restore admin action button styling (#233)."""

from __future__ import annotations

import random
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app import admin_contacts, admin_research_pages
from app.admin_layout import archive_action_button_class
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

COMPANY_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
CONTACT_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


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
                return css[brace_start : index + 1]
    raise AssertionError(f"Unclosed rule block for {selector_fragment!r}")


@pytest.mark.unit
def test_archive_action_button_class_maps_archive_and_restore() -> None:
    assert archive_action_button_class(archived=False) == (
        "admin-action admin-action--destructive"
    )
    assert archive_action_button_class(archived=True) == (
        "admin-action admin-action--secondary"
    )


@pytest.mark.unit
def test_admin_action_css_resets_native_button_appearance() -> None:
    css = _admin_css()
    base = _rule_block(css, ".admin-action {")
    for property_name in (
        "appearance: none",
        "font-family: inherit",
        "padding:",
        "border:",
        "border-radius:",
        "cursor: pointer",
    ):
        assert property_name in base
    destructive = _rule_block(css, ".admin-action--destructive {")
    assert "background:" in destructive
    assert "background: none" not in destructive
    assert "color:" in destructive
    secondary = _rule_block(css, ".admin-action--secondary {")
    assert "background:" in secondary
    assert ":disabled" in css
    assert ".admin-action--destructive:hover" in css
    assert ".admin-action--destructive:focus-visible" in css
    assert ".admin-action--secondary:focus-visible" in css


@pytest.mark.unit
def test_company_research_page_uses_semantic_archive_classes() -> None:
    archive_html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme"},
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert 'class="admin-action admin-action--destructive"' in archive_html
    assert "Archive company" in archive_html
    assert 'class="admin-exit" type="submit">Archive company' not in archive_html

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
def test_contact_research_and_edit_pages_use_semantic_archive_classes() -> None:
    contact = {
        "id": CONTACT_ID,
        "full_name": "Pat Example",
        "company_id": COMPANY_ID,
        "buying_roles": ["founder"],
    }
    detail_html = admin_research_pages.render_admin_contact_research_page(
        contact=contact,
        company={"id": COMPANY_ID, "name": "Acme"},
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert 'class="admin-action admin-action--destructive"' in detail_html
    assert "Archive contact" in detail_html

    edit_html = admin_contacts.render_contact_form_page(
        admin_username="operator",
        csrf_token="csrf-token",
        companies=[{"id": COMPANY_ID, "name": "Acme"}],
        contact=contact,
    )
    assert 'class="admin-action admin-action--destructive"' in edit_html

    restore_html = admin_contacts.render_contact_form_page(
        admin_username="operator",
        csrf_token="csrf-token",
        companies=[],
        contact={**contact, "archived_at": "2026-01-01"},
    )
    assert 'class="admin-action admin-action--secondary"' in restore_html
    assert "Restore contact" in restore_html


@pytest.mark.unit
def test_preview_company_and_contact_detail_fixtures_are_seed_stable() -> None:
    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    archive_company_a = build_preview_company_research(
        PREVIEW_COMPANY_ARCHIVE_ID, rng=random.Random(233), now=now
    )
    archive_company_b = build_preview_company_research(
        PREVIEW_COMPANY_ARCHIVE_ID, rng=random.Random(233), now=now
    )
    restore_company = build_preview_company_research(
        PREVIEW_COMPANY_RESTORE_ID, rng=random.Random(233), now=now
    )
    assert archive_company_a is not None
    assert archive_company_b is not None
    assert restore_company is not None
    assert archive_company_a == archive_company_b
    assert archive_company_a[0]["archived_at"] is None
    assert restore_company[0]["archived_at"] is not None

    archive_contact = build_preview_contact_research(
        PREVIEW_CONTACT_ARCHIVE_ID, rng=random.Random(233), now=now
    )
    restore_contact = build_preview_contact_research(
        PREVIEW_CONTACT_RESTORE_ID, rng=random.Random(233), now=now
    )
    assert archive_contact is not None
    assert restore_contact is not None
    assert archive_contact[0]["archived_at"] is None
    assert restore_contact[0]["archived_at"] is not None

    edit_restore = build_preview_contact_edit(
        PREVIEW_CONTACT_RESTORE_ID, rng=random.Random(233), now=now
    )
    assert edit_restore is not None
    assert len(edit_restore[1]) >= 2


@pytest.mark.unit
@pytest.mark.integration
def test_preview_routes_render_archive_and_restore_buttons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    client = TestClient(app, follow_redirects=False)
    cases = (
        (f"/admin/companies/{PREVIEW_COMPANY_ARCHIVE_ID}", "Archive company", "destructive"),
        (f"/admin/companies/{PREVIEW_COMPANY_RESTORE_ID}", "Restore company", "secondary"),
        (f"/admin/contacts/{PREVIEW_CONTACT_ARCHIVE_ID}", "Archive contact", "destructive"),
        (f"/admin/contacts/{PREVIEW_CONTACT_RESTORE_ID}", "Restore contact", "secondary"),
        (
            f"/admin/contacts/{PREVIEW_CONTACT_ARCHIVE_ID}/edit",
            "Archive contact",
            "destructive",
        ),
        (
            f"/admin/contacts/{PREVIEW_CONTACT_RESTORE_ID}/edit",
            "Restore contact",
            "secondary",
        ),
    )
    for path, label, variant in cases:
        response = client.get(path)
        assert response.status_code == 200, path
        body = response.text
        assert label in body, path
        assert f'admin-action--{variant}' in body, path
        assert 'class="admin-exit" type="submit"' not in body, path
