"""Archive/restore admin action button styling (#233)."""

from __future__ import annotations

import re
from pathlib import Path
from uuid import UUID

import pytest

from app import admin_contacts
from app.admin_layout import archive_action_button_class

ADMIN_CSS = Path(__file__).resolve().parents[1] / "site/assets/admin.css"

COMPANY_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
CONTACT_ID = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")


def _css_rule_block(css: str, selector: str) -> str:
    pattern = re.escape(selector) + r"\s*\{([^}]*)\}"
    match = re.search(pattern, css)
    assert match is not None, f"missing CSS rule for {selector}"
    return match.group(1)


def _css_has_property(rule_block: str, property_name: str) -> bool:
    return re.search(rf"(?<![\w-]){re.escape(property_name)}\s*:", rule_block) is not None


@pytest.mark.unit
def test_archive_action_button_class_semantics() -> None:
    assert archive_action_button_class(is_archived=False) == (
        "admin-action admin-action--destructive"
    )
    assert archive_action_button_class(is_archived=True) == "admin-action admin-action--restore"


@pytest.mark.unit
def test_company_research_page_archive_and_restore_markup() -> None:
    from app.admin_research_pages import render_admin_company_research_page

    archive_html = render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme"},
        contacts=[],
        records=[],
        csrf_token="csrf-token",
    )
    assert 'class="admin-action admin-action--destructive" type="submit">Archive company</button>' in (
        archive_html
    )
    assert 'class="admin-exit" type="submit">Archive company</button>' not in archive_html

    restore_html = render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme", "archived_at": "2026-01-01"},
        contacts=[],
        records=[],
        csrf_token="csrf-token",
    )
    assert 'class="admin-action admin-action--restore" type="submit">Restore company</button>' in (
        restore_html
    )


@pytest.mark.unit
def test_contact_research_and_edit_archive_and_restore_markup() -> None:
    from app.admin_research_pages import render_admin_contact_research_page

    contact = {"id": CONTACT_ID, "full_name": "Pat Example"}
    archive_detail = render_admin_contact_research_page(
        contact=contact,
        company=None,
        records=[],
        csrf_token="csrf-token",
    )
    assert 'class="admin-action admin-action--destructive" type="submit">Archive contact</button>' in (
        archive_detail
    )

    restore_detail = render_admin_contact_research_page(
        contact={**contact, "archived_at": "2026-01-01"},
        company=None,
        records=[],
        csrf_token="csrf-token",
    )
    assert 'class="admin-action admin-action--restore" type="submit">Restore contact</button>' in (
        restore_detail
    )

    archive_edit = admin_contacts.render_contact_form_page(
        admin_username="operator",
        csrf_token="csrf-token",
        companies=[],
        contact=contact,
    )
    assert 'class="admin-action admin-action--destructive" type="submit">Archive contact</button>' in (
        archive_edit
    )

    restore_edit = admin_contacts.render_contact_form_page(
        admin_username="operator",
        csrf_token="csrf-token",
        companies=[],
        contact={**contact, "archived_at": "2026-01-01"},
    )
    assert 'class="admin-action admin-action--restore" type="submit">Restore contact</button>' in (
        restore_edit
    )


@pytest.mark.unit
def test_admin_action_css_resets_native_button_appearance() -> None:
    css = ADMIN_CSS.read_text(encoding="utf-8")
    action_block = _css_rule_block(css, ".admin-action")
    for prop in (
        "background",
        "border",
        "padding",
        "font-family",
        "color",
        "cursor",
        "border-radius",
    ):
        assert _css_has_property(action_block, prop), f".admin-action missing {prop}"

    exit_block = _css_rule_block(css, ".admin-exit")
    assert not _css_has_property(exit_block, "background")
    assert not _css_has_property(exit_block, "padding")


@pytest.mark.unit
def test_admin_action_css_covers_interaction_and_disabled_states() -> None:
    css = ADMIN_CSS.read_text(encoding="utf-8")
    assert ".admin-action:hover" in css
    assert ".admin-action:focus-visible" in css
    assert ".admin-action:active:not(:disabled)" in css
    assert ".admin-action:disabled" in css
    assert '.admin-action[aria-disabled="true"]' in css

    destructive_block = _css_rule_block(css, ".admin-action--destructive")
    restore_block = _css_rule_block(css, ".admin-action--restore")
    assert _css_has_property(destructive_block, "border-color")
    assert _css_has_property(restore_block, "border-color")
    assert destructive_block != restore_block

    disabled_match = re.search(
        r"\.admin-action:disabled,\s*\n\.admin-action\[aria-disabled=\"true\"\]\s*\{([^}]*)\}",
        css,
    )
    assert disabled_match is not None
    disabled_block = disabled_match.group(1)
    assert "opacity" in disabled_block
    assert "cursor: not-allowed" in disabled_block


@pytest.mark.unit
def test_admin_action_css_avoids_native_white_button_palette() -> None:
    css = ADMIN_CSS.read_text(encoding="utf-8")
    action_block = _css_rule_block(css, ".admin-action")
    destructive_block = _css_rule_block(css, ".admin-action--destructive")
    restore_block = _css_rule_block(css, ".admin-action--restore")

    for block in (action_block, destructive_block, restore_block):
        assert "background: #fff" not in block
        assert "background: white" not in block
        assert "background-color: #fff" not in block
        assert "background-color: white" not in block

    assert "var(--surface)" in action_block or "color-mix" in action_block
    assert "#e05a5a" in destructive_block or "color-mix" in destructive_block
