"""Responsive admin form width variants (#238)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import admin_companies, admin_contacts, admin_pages, admin_research_pages
from app.admin_auth import SESSION_COOKIE_NAME
from app.admin_pipeline_pages import render_pipeline_detail_page
from app.admin_preview import PREVIEW_PIPELINE_COMPANY_IDS
from app.main import app
from tests.conftest import enable_admin_preview_env

SITE_CSS = Path(__file__).resolve().parents[1] / "site/assets/site.css"
ADMIN_CSS = Path(__file__).resolve().parents[1] / "site/assets/admin.css"

COMPANY_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
CONTACT_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

client = TestClient(app, follow_redirects=False)


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


def _media_block(css: str, query_fragment: str) -> str:
    start = css.index(query_fragment)
    brace_start = css.index("{", start)
    depth = 0
    for index, char in enumerate(css[brace_start:], start=brace_start):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return css[start : index + 1]
    raise AssertionError(f"Unclosed media query for {query_fragment!r}")


def _label_associations(html: str) -> list[tuple[str, str]]:
    """Return (for_id, input_id) pairs for explicit label associations."""
    pairs: list[tuple[str, str]] = []
    for match in re.finditer(
        r'<label\b([^>]*)>(.*?)</label>',
        html,
        flags=re.DOTALL | re.IGNORECASE,
    ):
        attrs = match.group(1)
        for_attr = re.search(r'\bfor="([^"]+)"', attrs)
        if not for_attr:
            continue
        for_id = for_attr.group(1)
        field = match.group(2)
        id_match = re.search(r'\bid="([^"]+)"', field)
        if id_match:
            pairs.append((for_id, id_match.group(1)))
    for match in re.finditer(
        r'<div class="field"[^>]*>\s*<label\b[^>]*\bfor="([^"]+)"[^>]*>.*?</label>\s*<(?:input|select|textarea)\b[^>]*\bid="([^"]+)"',
        html,
        flags=re.DOTALL | re.IGNORECASE,
    ):
        pairs.append((match.group(1), match.group(2)))
    return pairs


@pytest.mark.unit
def test_admin_form_base_is_full_width_without_global_cap() -> None:
    css = SITE_CSS.read_text(encoding="utf-8")
    block = _rule_block(css, ".admin-form {")
    assert "max-width: 100%" in block
    assert "width: 100%" in block
    assert "max-width: 36ch" not in block


@pytest.mark.unit
def test_admin_form_compact_variant_stays_narrow_for_login_and_filters() -> None:
    css = SITE_CSS.read_text(encoding="utf-8")
    block = _rule_block(css, ".admin-form--compact")
    assert "max-width: min(100%, 36ch)" in block


@pytest.mark.unit
def test_admin_form_editor_variant_widens_on_desktop_only() -> None:
    css = ADMIN_CSS.read_text(encoding="utf-8")
    base_block = _rule_block(css, ".admin-form--editor {")
    assert "max-width: 100%" in base_block

    desktop_block = _media_block(css, "@media (min-width: 48rem)")
    assert ".admin-form--editor" in desktop_block
    assert "max-width: min(100%, 72ch)" in desktop_block


@pytest.mark.unit
def test_research_record_list_widens_on_desktop() -> None:
    css = ADMIN_CSS.read_text(encoding="utf-8")
    base_block = _rule_block(css, ".research-record-list {")
    assert "max-width: 100%" in base_block

    desktop_block = _media_block(css, "@media (min-width: 48rem)")
    assert ".research-record-list" in desktop_block
    assert "max-width: min(100%, 72ch)" in desktop_block


@pytest.mark.unit
def test_admin_form_controls_stay_within_container() -> None:
    css = SITE_CSS.read_text(encoding="utf-8")
    block = _rule_block(css, ".admin-form textarea,")
    assert "width: 100%" in block
    assert "box-sizing: border-box" in _rule_block(css, ".admin-form {")


@pytest.mark.unit
def test_login_form_uses_compact_variant() -> None:
    html = admin_pages.render_admin_login_page(csrf_token="csrf")
    assert 'class="admin-form admin-form--compact"' in html


@pytest.mark.unit
def test_company_and_contact_forms_use_editor_variant() -> None:
    company_html = admin_companies.render_company_form_page(
        csrf_token="csrf",
        admin_username="operator",
        company={"id": COMPANY_ID, "name": "Acme"},
    )
    assert 'class="admin-form admin-form--editor"' in company_html

    contact_html = admin_contacts.render_contact_form_page(
        csrf_token="csrf",
        admin_username="operator",
        companies=[{"id": COMPANY_ID, "name": "Acme"}],
        contact={"id": CONTACT_ID, "full_name": "Ada", "buying_roles": []},
    )
    assert 'class="admin-form admin-form--editor"' in contact_html


@pytest.mark.unit
def test_list_filter_forms_use_compact_variant() -> None:
    companies_html = admin_companies.render_companies_list_page(
        admin_username="operator",
        csrf_token="csrf",
        filters={
            "q": None,
            "category": None,
            "stage": None,
            "target_status": None,
            "freshness": None,
            "archived": None,
        },
        companies=[],
    )
    assert 'class="admin-form admin-form--compact"' in companies_html

    contacts_html = admin_contacts.render_contacts_list_page(
        admin_username="operator",
        csrf_token="csrf",
        filters={"q": None, "company_id": None, "buying_role": None, "archived": None},
        contacts=[],
        companies=[],
    )
    assert 'class="admin-form admin-form--compact"' in contacts_html


@pytest.mark.unit
def test_pipeline_detail_forms_use_editor_variant() -> None:
    html = render_pipeline_detail_page(
        company={
            "id": PREVIEW_PIPELINE_COMPANY_IDS[0],
            "name": "Northwind Labs",
            "pipeline_stage": "contacted",
            "next_action": "Follow up",
            "next_action_due_at": None,
            "pipeline_owner": "alex",
            "expected_value_cents": 75_000,
        },
        history=[],
        activities=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert html.count('class="admin-form admin-form--editor"') == 3


@pytest.mark.unit
def test_research_forms_use_editor_variant() -> None:
    company_html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme"},
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert 'class="admin-form admin-form--editor research-form"' in company_html

    contact_html = admin_research_pages.render_admin_contact_research_page(
        contact={
            "id": CONTACT_ID,
            "full_name": "Ada",
            "company_id": COMPANY_ID,
            "buying_roles": [],
        },
        company={"id": COMPANY_ID, "name": "Acme"},
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert 'class="admin-form admin-form--editor research-form"' in contact_html


@pytest.mark.unit
def test_company_form_label_associations_unchanged() -> None:
    html = admin_companies.render_company_form_page(
        csrf_token="csrf",
        admin_username="operator",
        company={"id": COMPANY_ID, "name": "Acme"},
    )
    pairs = _label_associations(html)
    assert ("name", "name") in pairs
    assert ("domain", "domain") in pairs
    assert ("website", "website") in pairs
    assert ("notes", "notes") in pairs
    assert all(for_id == input_id for for_id, input_id in pairs)


@pytest.mark.unit
def test_contact_form_label_associations_unchanged() -> None:
    html = admin_contacts.render_contact_form_page(
        csrf_token="csrf",
        admin_username="operator",
        companies=[{"id": COMPANY_ID, "name": "Acme"}],
        contact={"id": CONTACT_ID, "full_name": "Ada", "buying_roles": []},
    )
    pairs = _label_associations(html)
    assert ("full_name", "full_name") in pairs
    assert ("email", "email") in pairs
    assert ("profile_url", "profile_url") in pairs
    assert all(for_id == input_id for for_id, input_id in pairs)


@pytest.mark.unit
def test_pipeline_detail_label_associations_unchanged() -> None:
    html = render_pipeline_detail_page(
        company={
            "id": PREVIEW_PIPELINE_COMPANY_IDS[0],
            "name": "Northwind Labs",
            "pipeline_stage": "contacted",
            "next_action": "Follow up",
            "next_action_due_at": None,
            "pipeline_owner": "alex",
            "expected_value_cents": 75_000,
        },
        history=[],
        activities=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    pairs = _label_associations(html)
    assert ("next_action", "next_action") in pairs
    assert ("next_action_due_at", "next_action_due_at") in pairs
    assert ("pipeline_owner", "pipeline_owner") in pairs
    assert ("expected_value_cents", "expected_value_cents") in pairs
    assert all(for_id == input_id for for_id, input_id in pairs)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("viewport_query", "expected_max_width"),
    [
        ("base", "100%"),
        ("@media (min-width: 48rem)", "72ch"),
    ],
)
def test_editor_form_responsive_breakpoints(
    viewport_query: str,
    expected_max_width: str,
) -> None:
    css = ADMIN_CSS.read_text(encoding="utf-8")
    if viewport_query == "base":
        block = _rule_block(css, ".admin-form--editor {")
    else:
        block = _media_block(css, viewport_query)
    assert expected_max_width in block


@pytest.mark.unit
@pytest.mark.integration
def test_preview_pipeline_detail_renders_editor_forms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enable_admin_preview_env(monkeypatch)
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "42")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    response = client.get(
        f"/admin/pipeline/{PREVIEW_PIPELINE_COMPANY_IDS[0]}",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert response.status_code == 200
    body = response.text
    assert body.count('class="admin-form admin-form--editor"') == 3
    assert 'href="/assets/admin.css"' in body
