"""Tests for archive/restore admin action button styling (#233)."""

from __future__ import annotations

import random
from pathlib import Path
from uuid import UUID

import pytest

from app import admin_contacts, admin_research_pages
from app.admin_layout import admin_archive_action_class

ADMIN_CSS = Path(__file__).resolve().parents[1] / "site/assets/admin.css"

COMPANY_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
CONTACT_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


@pytest.mark.unit
def test_admin_archive_action_class_variants() -> None:
    assert admin_archive_action_class(archived=False) == "admin-action admin-action--destructive"
    assert admin_archive_action_class(archived=True) == "admin-action admin-action--restore"


@pytest.mark.unit
def test_company_research_page_uses_semantic_archive_action_classes() -> None:
    active_html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme"},
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert 'class="admin-action admin-action--destructive" type="submit">Archive company' in active_html
    assert 'admin-exit" type="submit">Archive' not in active_html

    archived_html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme", "archived_at": "2026-01-01"},
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert 'class="admin-action admin-action--restore" type="submit">Restore company' in archived_html


@pytest.mark.unit
def test_contact_research_page_uses_semantic_archive_action_classes() -> None:
    active_html = admin_research_pages.render_admin_contact_research_page(
        contact={"id": CONTACT_ID, "full_name": "Pat", "buying_roles": []},
        company={"id": COMPANY_ID, "name": "Acme"},
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
        company={"id": COMPANY_ID, "name": "Acme"},
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert 'class="admin-action admin-action--restore" type="submit">Restore contact' in archived_html


@pytest.mark.unit
def test_contact_edit_page_uses_semantic_archive_action_classes() -> None:
    active_html = admin_contacts.render_contact_form_page(
        csrf_token="csrf",
        admin_username="operator",
        companies=[{"id": COMPANY_ID, "name": "Acme"}],
        contact={"id": CONTACT_ID, "full_name": "Pat"},
    )
    assert 'class="admin-action admin-action--destructive" type="submit">Archive contact' in active_html
    assert 'admin-exit" type="submit">Archive' not in active_html

    archived_html = admin_contacts.render_contact_form_page(
        csrf_token="csrf",
        admin_username="operator",
        companies=[],
        contact={"id": CONTACT_ID, "full_name": "Pat", "archived_at": "2026-01-01"},
    )
    assert 'class="admin-action admin-action--restore" type="submit">Restore contact' in archived_html


@pytest.mark.unit
def test_admin_action_css_resets_native_button_appearance() -> None:
    css = ADMIN_CSS.read_text(encoding="utf-8")
    action_block = css.split(".admin-action {", 1)[1].split("}", 1)[0]
    assert "appearance: none" in action_block
    assert "background: transparent" in action_block
    assert "border:" in action_block
    assert "padding:" in action_block
    assert "cursor: pointer" in action_block
    assert "border-radius:" in action_block
    assert "font-family: inherit" in action_block

    destructive_block = css.split(".admin-action--destructive {", 1)[1].split("}", 1)[0]
    restore_block = css.split(".admin-action--restore {", 1)[1].split("}", 1)[0]
    assert "background:" in destructive_block
    assert destructive_block.strip() != "background: transparent;"
    assert "background:" in restore_block

    disabled_block = css.split(".admin-action:disabled {", 1)[1].split("}", 1)[0]
    assert "cursor: not-allowed" in disabled_block
    assert "opacity:" in disabled_block

    assert ".admin-action:focus-visible" in css
    assert ".admin-action--destructive:hover:not(:disabled)" in css
    assert ".admin-action--restore:hover:not(:disabled)" in css
    assert ".admin-action:active:not(:disabled)" in css


@pytest.mark.unit
def test_admin_exit_keeps_link_styling_separate_from_form_actions() -> None:
    css = ADMIN_CSS.read_text(encoding="utf-8")
    exit_block = css.split(".admin-exit {", 1)[1].split("}", 1)[0]
    assert "background:" not in exit_block
    assert "padding:" not in exit_block
    assert "border-bottom:" in exit_block


@pytest.mark.unit
def test_preview_company_detail_pages_include_archive_action_buttons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.admin_preview import (
        PREVIEW_COMPANY_ARCHIVE_DETAIL_ID,
        PREVIEW_COMPANY_RESTORE_DETAIL_ID,
        build_preview_company_detail_page,
    )

    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "7")
    archive = build_preview_company_detail_page(PREVIEW_COMPANY_ARCHIVE_DETAIL_ID, rng=random.Random(7))
    restore = build_preview_company_detail_page(PREVIEW_COMPANY_RESTORE_DETAIL_ID, rng=random.Random(7))
    assert archive is not None and restore is not None
    company, contacts, records = archive
    assert company.get("archived_at") is None
    assert contacts
    assert records
    restore_company, _, _ = restore
    assert restore_company.get("archived_at") is not None


@pytest.mark.unit
def test_preview_contact_detail_and_edit_pages_include_restore_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.admin_preview import (
        PREVIEW_CONTACT_ARCHIVE_DETAIL_ID,
        PREVIEW_CONTACT_RESTORE_DETAIL_ID,
        build_preview_contact_detail_page,
        build_preview_contact_edit_page,
    )

    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "7")
    archive = build_preview_contact_detail_page(
        PREVIEW_CONTACT_ARCHIVE_DETAIL_ID, rng=random.Random(7)
    )
    restore = build_preview_contact_edit_page(
        PREVIEW_CONTACT_RESTORE_DETAIL_ID, rng=random.Random(7)
    )
    assert archive is not None and restore is not None
    contact, company, records = archive
    assert contact.get("archived_at") is None
    assert company is not None
    assert records
    restore_contact, companies = restore
    assert restore_contact.get("archived_at") is not None
    assert companies
