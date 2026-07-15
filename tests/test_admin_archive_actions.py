"""Archive/Restore admin action button styling (#233)."""

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
from app.admin_layout import admin_archive_action_classes
from app.admin_preview import (
    PREVIEW_CRM_COMPANY_ARCHIVE_ID,
    PREVIEW_CRM_COMPANY_RESTORE_ID,
    PREVIEW_CRM_CONTACT_ARCHIVE_ID,
    PREVIEW_CRM_CONTACT_RESTORE_ID,
    build_preview_company_research,
    build_preview_contact_edit,
    build_preview_contact_research,
)
from app.main import app

ADMIN_CSS = Path(__file__).resolve().parents[1] / "site/assets/admin.css"
COMPANY_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
CONTACT_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


def _rule_block(css: str, selector_fragment: str) -> str:
    start = css.index(selector_fragment)
    brace = css.index("{", start)
    depth = 0
    for index, char in enumerate(css[brace:], start=brace):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return css[start : index + 1]
    raise AssertionError(f"Unclosed rule for {selector_fragment!r}")


@pytest.mark.unit
def test_admin_archive_action_classes_map_archive_and_restore() -> None:
    assert admin_archive_action_classes(is_archived=False) == (
        "admin-action admin-action--destructive"
    )
    assert admin_archive_action_classes(is_archived=True) == (
        "admin-action admin-action--secondary"
    )


@pytest.mark.unit
def test_company_research_page_uses_semantic_archive_classes() -> None:
    active_html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme"},
        contacts=[],
        records=[],
        csrf_token="csrf",
    )
    assert 'class="admin-action admin-action--destructive" type="submit">Archive company' in active_html
    assert 'class="admin-exit" type="submit">Archive company' not in active_html

    archived_html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme", "archived_at": "2026-01-01"},
        contacts=[],
        records=[],
        csrf_token="csrf",
    )
    assert 'class="admin-action admin-action--secondary" type="submit">Restore company' in archived_html


@pytest.mark.unit
def test_contact_research_and_edit_pages_use_semantic_archive_classes() -> None:
    contact = {
        "id": CONTACT_ID,
        "full_name": "Pat Example",
        "company_id": COMPANY_ID,
        "buying_roles": [],
    }
    detail_html = admin_research_pages.render_admin_contact_research_page(
        contact=contact,
        company={"id": COMPANY_ID, "name": "Acme"},
        records=[],
        csrf_token="csrf",
    )
    assert 'class="admin-action admin-action--destructive" type="submit">Archive contact' in detail_html

    edit_html = admin_contacts.render_contact_form_page(
        admin_username="operator",
        csrf_token="csrf",
        companies=[{"id": COMPANY_ID, "name": "Acme"}],
        contact=contact,
    )
    assert 'class="admin-action admin-action--destructive" type="submit">Archive contact' in edit_html

    restore_html = admin_contacts.render_contact_form_page(
        admin_username="operator",
        csrf_token="csrf",
        companies=[],
        contact={**contact, "archived_at": "2026-01-01"},
    )
    assert 'class="admin-action admin-action--secondary" type="submit">Restore contact' in restore_html


@pytest.mark.unit
def test_admin_action_css_resets_native_button_appearance() -> None:
    css = ADMIN_CSS.read_text(encoding="utf-8")
    base = _rule_block(css, ".admin-action {")
    destructive = _rule_block(css, ".admin-action--destructive {")
    secondary = _rule_block(css, ".admin-action--secondary {")
    disabled = _rule_block(css, ".admin-action:disabled,")

    assert "background:" in base
    assert "padding:" in base
    assert "cursor: pointer" in base
    assert "border:" in base
    assert "border-radius:" in base
    assert "background:" in destructive
    assert "background:" in secondary
    assert "border-color:" in destructive
    assert "border-color:" in secondary

    assert "appearance: none" in base
    assert "font-family: inherit" in base
    assert "#e05a5a" in destructive or "#c94a4a" in destructive
    assert "outline:" in _rule_block(css, ".admin-action:focus-visible {")
    assert ":hover" in css.split(".admin-action--destructive {", 1)[1]
    assert ":active" in css.split(".admin-action--destructive {", 1)[1]
    assert "opacity:" in disabled
    assert "cursor: not-allowed" in disabled


@pytest.mark.unit
def test_preview_crm_archive_restore_builders_stable_with_seed() -> None:
    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    company_archive = build_preview_company_research(
        PREVIEW_CRM_COMPANY_ARCHIVE_ID,
        rng=random.Random(42),
        now=now,
    )
    company_restore = build_preview_company_research(
        PREVIEW_CRM_COMPANY_RESTORE_ID,
        rng=random.Random(42),
        now=now,
    )
    contact_archive = build_preview_contact_research(
        PREVIEW_CRM_CONTACT_ARCHIVE_ID,
        rng=random.Random(42),
        now=now,
    )
    contact_restore = build_preview_contact_research(
        PREVIEW_CRM_CONTACT_RESTORE_ID,
        rng=random.Random(42),
        now=now,
    )
    contact_edit = build_preview_contact_edit(
        PREVIEW_CRM_CONTACT_ARCHIVE_ID,
        rng=random.Random(42),
        now=now,
    )

    assert company_archive is not None
    assert company_restore is not None
    assert contact_archive is not None
    assert contact_restore is not None
    assert contact_edit is not None

    active_company, _, _ = company_archive
    archived_company, _, _ = company_restore
    assert active_company["archived_at"] is None
    assert archived_company["archived_at"] is not None

    active_contact, _, _ = contact_archive
    archived_contact, _, _ = contact_restore
    assert active_contact["archived_at"] is None
    assert archived_contact["archived_at"] is not None


@pytest.mark.unit
def test_preview_routes_render_archive_and_restore_action_buttons(
    monkeypatch: pytest.MonkeyPatch,
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

    client = TestClient(app, follow_redirects=False)
    routes = (
        (f"/admin/companies/{PREVIEW_CRM_COMPANY_ARCHIVE_ID}", "Archive company", "destructive"),
        (f"/admin/companies/{PREVIEW_CRM_COMPANY_RESTORE_ID}", "Restore company", "secondary"),
        (f"/admin/contacts/{PREVIEW_CRM_CONTACT_ARCHIVE_ID}", "Archive contact", "destructive"),
        (f"/admin/contacts/{PREVIEW_CRM_CONTACT_RESTORE_ID}", "Restore contact", "secondary"),
        (
            f"/admin/contacts/{PREVIEW_CRM_CONTACT_ARCHIVE_ID}/edit",
            "Archive contact",
            "destructive",
        ),
        (
            f"/admin/contacts/{PREVIEW_CRM_CONTACT_RESTORE_ID}/edit",
            "Restore contact",
            "secondary",
        ),
    )
    for path, label, variant in routes:
        response = client.get(path, cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"})
        assert response.status_code == 200, path
        assert label in response.text
        assert f'class="admin-action admin-action--{variant}" type="submit">{label}' in response.text
        assert f'class="admin-exit" type="submit">{label}' not in response.text
