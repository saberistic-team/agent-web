"""Archive/Restore admin action button styling (#233)."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app import admin_contacts, admin_research_pages
from app.admin_auth import SESSION_COOKIE_NAME
from app.admin_layout import admin_archive_action_button_class
from app.admin_preview import (
    PREVIEW_COMPANY_ACTIVE_ID,
    PREVIEW_COMPANY_ARCHIVED_ID,
    PREVIEW_CONTACT_ACTIVE_ID,
    PREVIEW_CONTACT_ARCHIVED_ID,
    build_preview_company_detail,
    build_preview_contact_detail,
    build_preview_contact_edit,
)
from app.main import app

ADMIN_CSS = Path(__file__).resolve().parents[1] / "site/assets/admin.css"
COMPANY_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
CONTACT_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
client = TestClient(app, follow_redirects=False)


@pytest.mark.unit
def test_admin_archive_action_button_class_variants() -> None:
    assert admin_archive_action_button_class(archived=False) == (
        "admin-action-btn admin-action-btn--archive"
    )
    assert admin_archive_action_button_class(archived=True) == (
        "admin-action-btn admin-action-btn--restore"
    )


@pytest.mark.unit
def test_company_research_page_renders_archive_action_class() -> None:
    html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme"},
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert 'class="admin-action-btn admin-action-btn--archive"' in html
    assert "Archive company" in html
    assert 'admin-exit" type="submit">Archive company' not in html


@pytest.mark.unit
def test_company_research_page_renders_restore_action_class() -> None:
    html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme", "archived_at": "2026-01-01"},
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert 'class="admin-action-btn admin-action-btn--restore"' in html
    assert "Restore company" in html


@pytest.mark.unit
def test_contact_research_page_renders_archive_and_restore_classes() -> None:
    archive_html = admin_research_pages.render_admin_contact_research_page(
        contact={"id": CONTACT_ID, "full_name": "Pat", "buying_roles": []},
        company=None,
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert 'class="admin-action-btn admin-action-btn--archive"' in archive_html
    assert "Archive contact" in archive_html

    restore_html = admin_research_pages.render_admin_contact_research_page(
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
    assert 'class="admin-action-btn admin-action-btn--restore"' in restore_html
    assert "Restore contact" in restore_html


@pytest.mark.unit
def test_contact_edit_page_renders_archive_action_classes() -> None:
    archive_html = admin_contacts.render_contact_form_page(
        admin_username="operator",
        csrf_token="csrf",
        companies=[],
        contact={"id": CONTACT_ID, "full_name": "Pat"},
    )
    assert 'class="admin-action-btn admin-action-btn--archive"' in archive_html
    assert "Archive contact" in archive_html

    restore_html = admin_contacts.render_contact_form_page(
        admin_username="operator",
        csrf_token="csrf",
        companies=[],
        contact={"id": CONTACT_ID, "full_name": "Pat", "archived_at": "2026-01-01"},
    )
    assert 'class="admin-action-btn admin-action-btn--restore"' in restore_html
    assert "Restore contact" in restore_html


@pytest.mark.unit
def test_admin_action_btn_css_resets_native_button_appearance() -> None:
    css = ADMIN_CSS.read_text(encoding="utf-8")
    block = css.split(".admin-action-btn {", 1)[1].split("}", 1)[0]
    assert "background:" in block
    assert "border:" in block
    assert "padding:" in block
    assert "font-family: inherit" in block
    assert "cursor: pointer" in block
    assert "border-radius:" in block
    assert "color:" in block
    assert "buttonface" not in block.lower()
    assert "#fff" not in block
    assert "#ffffff" not in block.lower()

    archive_block = css.split(".admin-action-btn--archive {", 1)[1].split("}", 1)[0]
    restore_block = css.split(".admin-action-btn--restore {", 1)[1].split("}", 1)[0]
    assert "background:" in archive_block
    assert "background:" in restore_block
    assert ":focus-visible" in css.split(".admin-action-btn--archive", 1)[1]
    assert ":disabled" in css.split(".admin-action-btn", 1)[1]


@pytest.mark.unit
def test_admin_exit_keeps_link_styling_separate_from_action_buttons() -> None:
    css = ADMIN_CSS.read_text(encoding="utf-8")
    exit_block = css.split(".admin-exit {", 1)[1].split("}", 1)[0]
    assert "background:" not in exit_block
    assert "padding:" not in exit_block
    assert ".admin-action-btn" in css
    assert ".admin-signout" in css


@pytest.mark.unit
def test_preview_company_detail_seed_stable() -> None:
    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    import random

    a = build_preview_company_detail(PREVIEW_COMPANY_ACTIVE_ID, rng=random.Random(42), now=now)
    b = build_preview_company_detail(PREVIEW_COMPANY_ACTIVE_ID, rng=random.Random(42), now=now)
    assert a == b
    assert a is not None
    company, contacts, records = a
    assert company["archived_at"] is None
    assert contacts
    assert records


@pytest.mark.unit
def test_preview_contact_detail_and_edit_cover_archive_states() -> None:
    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    import random

    active = build_preview_contact_detail(
        PREVIEW_CONTACT_ACTIVE_ID, rng=random.Random(7), now=now
    )
    archived = build_preview_contact_detail(
        PREVIEW_CONTACT_ARCHIVED_ID, rng=random.Random(7), now=now
    )
    assert active is not None and archived is not None
    assert active[0]["archived_at"] is None
    assert archived[0]["archived_at"] is not None

    edit = build_preview_contact_edit(PREVIEW_CONTACT_ARCHIVED_ID, rng=random.Random(7), now=now)
    assert edit is not None
    contact, companies = edit
    assert contact["archived_at"] is not None
    assert companies


@pytest.mark.unit
@pytest.mark.integration
def test_preview_routes_render_archive_and_restore_buttons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "42")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    cases = (
        (f"/admin/companies/{PREVIEW_COMPANY_ACTIVE_ID}", "Archive company", "--archive"),
        (f"/admin/companies/{PREVIEW_COMPANY_ARCHIVED_ID}", "Restore company", "--restore"),
        (f"/admin/contacts/{PREVIEW_CONTACT_ACTIVE_ID}", "Archive contact", "--archive"),
        (f"/admin/contacts/{PREVIEW_CONTACT_ARCHIVED_ID}", "Restore contact", "--restore"),
        (
            f"/admin/contacts/{PREVIEW_CONTACT_ACTIVE_ID}/edit",
            "Archive contact",
            "--archive",
        ),
        (
            f"/admin/contacts/{PREVIEW_CONTACT_ARCHIVED_ID}/edit",
            "Restore contact",
            "--restore",
        ),
    )
    for path, label, variant in cases:
        response = client.get(
            path,
            cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
        )
        assert response.status_code == 200, path
        body = response.text
        assert label in body, path
        assert f'class="admin-action-btn admin-action-btn{variant}"' in body, path
        assert re.search(
            rf'<button class="admin-action-btn admin-action-btn{variant}" type="submit">'
            rf"{re.escape(label)}</button>",
            body,
        ), path
