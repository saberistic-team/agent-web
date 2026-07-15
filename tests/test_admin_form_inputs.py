"""Regression tests for dark-themed admin text-like form controls (#231, #235).

Checkbox/radio accent-color, fieldset borders, and date/datetime color-scheme
theming are scoped to `site/assets/admin.css` (see `tests/test_admin_native_controls.py`),
not the public `site/assets/site.css`.
"""

from __future__ import annotations

import re
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app import admin_companies, admin_contacts, admin_pages, admin_research_pages
from app.admin_auth import SESSION_COOKIE_NAME
from app.admin_pipeline_pages import render_pipeline_detail_page
from app.admin_preview import PREVIEW_BRIEF_CONVERT_MATCHES_ID, PREVIEW_PIPELINE_COMPANY_IDS
from app.brief_service import BriefListFilters
from app.main import app

SITE_CSS = Path(__file__).resolve().parents[1] / "site/assets/site.css"
ADMIN_CSS = Path(__file__).resolve().parents[1] / "site/assets/admin.css"

TEXT_LIKE_INPUT_TYPES = (
    "text",
    "password",
    "url",
    "email",
    "number",
    "date",
    "datetime-local",
)

SELECTION_INPUT_TYPES = ("checkbox", "radio")

EXCLUDED_INPUT_TYPES = (
    "hidden",
    "file",
    "submit",
    "button",
    "image",
    "reset",
)

COMPANY_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
CONTACT_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

client = TestClient(app, follow_redirects=False)


def _site_css() -> str:
    return SITE_CSS.read_text(encoding="utf-8")


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


def _admin_form_input_types(html: str) -> dict[str, set[str]]:
    """Map admin-form blocks to input type attributes (empty string = untyped)."""
    forms = re.findall(
        r'<form class="admin-form[^"]*"[^>]*>(.*?)</form>',
        html,
        flags=re.DOTALL,
    )
    result: dict[str, set[str]] = {}
    for index, form in enumerate(forms):
        types: set[str] = set()
        for match in re.finditer(r"<input\b([^>]*)/?>", form):
            attrs = match.group(1)
            type_match = re.search(r'\btype="([^"]+)"', attrs)
            types.add(type_match.group(1) if type_match else "")
        result[f"form-{index}"] = types
    return result


def _text_like_inputs_in_admin_forms(html: str) -> list[str]:
    forms = re.findall(
        r'<form class="admin-form[^"]*"[^>]*>(.*?)</form>',
        html,
        flags=re.DOTALL,
    )
    inputs: list[str] = []
    for form in forms:
        for match in re.finditer(r"<input\b([^>]*)/?>", form):
            attrs = match.group(1)
            type_match = re.search(r'\btype="([^"]+)"', attrs)
            input_type = type_match.group(1) if type_match else ""
            if input_type in EXCLUDED_INPUT_TYPES or input_type in SELECTION_INPUT_TYPES:
                continue
            inputs.append(attrs)
    return inputs


@pytest.mark.unit
def test_admin_form_text_like_controls_use_dark_surface_tokens() -> None:
    css = _site_css()
    block = _rule_block(css, ".admin-form textarea,")
    assert "background: var(--surface)" in block
    assert "color: var(--ink)" in block
    assert "border: 1px solid var(--line)" in block
    assert "font-family: var(--mono)" in block
    assert "padding: 0.85rem 1rem" in block


@pytest.mark.unit
@pytest.mark.parametrize("input_type", TEXT_LIKE_INPUT_TYPES)
def test_admin_form_css_covers_text_like_input_type(input_type: str) -> None:
    css = _site_css()
    if input_type == "text":
        assert '.admin-form input[type="text"]' in css
    else:
        assert f'.admin-form input[type="{input_type}"]' in css


@pytest.mark.unit
def test_admin_form_css_covers_untyped_inputs() -> None:
    css = _site_css()
    assert ".admin-form input:not([type])" in css


@pytest.mark.unit
def test_admin_app_scopes_dark_color_scheme() -> None:
    css = _admin_css()
    block = _rule_block(css, "body.admin-app {")
    assert "color-scheme: dark" in block
    site = _site_css()
    assert "color-scheme" not in site.split(".brief-form")[0]


@pytest.mark.unit
def test_brief_filter_date_uses_dark_color_scheme() -> None:
    css = _admin_css()
    block = _rule_block(css, '.brief-filter input[type="date"]')
    assert "color-scheme: dark" in block


@pytest.mark.unit
def test_brief_convert_fieldset_and_choice_classes_themed() -> None:
    css = _admin_css()
    fieldset_block = _rule_block(css, ".brief-convert-fieldset {")
    assert "border: 1px solid var(--line)" in fieldset_block
    choice_block = _rule_block(css, ".brief-convert-choice,")
    assert "cursor: pointer" in choice_block
    assert "accent-color: var(--accent)" in css
    assert ".brief-convert-match" in css
    assert ".admin-checkbox" in css


@pytest.mark.unit
def test_public_brief_form_css_unthemed_for_selection_controls() -> None:
    css = _site_css()
    public_slice = css.split(".admin-form")[0]
    assert 'input[type="checkbox"]' not in public_slice
    assert 'input[type="radio"]' not in public_slice


@pytest.mark.unit
@pytest.mark.parametrize("excluded_type", EXCLUDED_INPUT_TYPES)
def test_admin_form_css_does_not_theme_excluded_input_type(excluded_type: str) -> None:
    css = _site_css()
    assert f'input[type="{excluded_type}"]' not in css.split(".research-form")[0]


@pytest.mark.unit
def test_admin_form_input_focus_visible_uses_accent_border() -> None:
    css = _site_css()
    block = _rule_block(css, ".admin-form textarea:focus-visible,")
    assert "outline: none" in block
    assert "border-color: var(--accent)" in block
    for input_type in TEXT_LIKE_INPUT_TYPES:
        if input_type == "text":
            assert '.admin-form input[type="text"]:focus-visible' in block
        else:
            assert f'.admin-form input[type="{input_type}"]:focus-visible' in block
    assert ".admin-form input:not([type]):focus-visible" in block


@pytest.mark.unit
def test_admin_form_input_placeholder_disabled_and_readonly_rules() -> None:
    css = _site_css()
    placeholder_block = _rule_block(css, ".admin-form textarea::placeholder,")
    assert "color: var(--muted)" in placeholder_block
    assert "opacity: 1" in placeholder_block
    assert ".admin-form input:not([type])::placeholder" in placeholder_block
    assert '.admin-form input[type="url"]::placeholder' in placeholder_block

    disabled_block = _rule_block(css, ".admin-form textarea:disabled,")
    assert "opacity: 0.65" in disabled_block
    assert "cursor: not-allowed" in disabled_block
    assert '.admin-form input[type="number"]:disabled' in disabled_block
    assert '.admin-form input[type="date"][readonly]' in disabled_block


@pytest.mark.unit
def test_admin_form_input_validation_and_autofill_rules() -> None:
    css = _site_css()
    invalid_block = _rule_block(css, ".admin-form input:not([type]):user-invalid,")
    assert "border-color: #e88a6a" in invalid_block
    assert ':invalid:not(:focus):not(:placeholder-shown)' in invalid_block

    autofill_block = _rule_block(css, ".admin-form input:not([type]):-webkit-autofill,")
    assert "-webkit-text-fill-color: var(--ink)" in autofill_block
    assert "-webkit-box-shadow: 0 0 0 1000px var(--surface) inset" in autofill_block
    assert "caret-color: var(--ink)" in autofill_block


@pytest.mark.unit
def test_site_css_asset_served_for_admin_shell() -> None:
    response = client.get("/assets/site.css")
    assert response.status_code == 200
    body = response.text
    assert ".admin-form input:not([type])" in body
    assert "background: var(--surface)" in body


@pytest.mark.unit
def test_pipeline_detail_renders_text_like_inputs_inside_admin_form() -> None:
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
    assert 'href="/assets/site.css"' in html
    form_types = _admin_form_input_types(html)
    assert form_types
    merged = set().union(*form_types.values())
    assert "" in merged  # Owner (untyped)
    assert "datetime-local" in merged
    assert "number" in merged
    assert "hidden" in merged
    assert "checkbox" in merged
    for attrs in _text_like_inputs_in_admin_forms(html):
        assert "style=" not in attrs


@pytest.mark.unit
def test_companies_list_search_field_is_untyped_admin_form_input() -> None:
    html = admin_companies.render_companies_list_page(
        admin_username="operator",
        csrf_token="csrf",
        filters={
            "q": "acme",
            "category": None,
            "stage": None,
            "target_status": None,
            "freshness": None,
            "archived": None,
        },
        companies=[],
    )
    inputs = _text_like_inputs_in_admin_forms(html)
    assert any('name="q"' in attrs and "type=" not in attrs for attrs in inputs)


@pytest.mark.unit
def test_company_form_renders_text_like_input_types() -> None:
    html = admin_companies.render_company_form_page(
        csrf_token="csrf",
        admin_username="operator",
        company={
            "id": COMPANY_ID,
            "name": "Acme",
            "domain": "acme.dev",
            "website": "https://acme.dev",
            "headcount_estimate": 42,
            "funding_summary": "Seed",
            "last_verified_at": "2026-07-01",
        },
    )
    merged = set().union(*_admin_form_input_types(html).values())
    assert "" in merged
    assert "url" in merged
    assert "number" in merged
    assert "date" in merged
    assert "hidden" in merged


@pytest.mark.unit
def test_contacts_list_and_form_render_text_like_inputs() -> None:
    list_html = admin_contacts.render_contacts_list_page(
        admin_username="operator",
        csrf_token="csrf",
        filters={
            "q": "ada",
            "company_id": None,
            "buying_role": None,
            "archived": None,
        },
        contacts=[],
        companies=[{"id": COMPANY_ID, "name": "Acme"}],
    )
    list_inputs = _text_like_inputs_in_admin_forms(list_html)
    assert any('name="q"' in attrs for attrs in list_inputs)

    form_html = admin_contacts.render_contact_form_page(
        csrf_token="csrf",
        admin_username="operator",
        companies=[{"id": COMPANY_ID, "name": "Acme"}],
        contact={
            "id": CONTACT_ID,
            "full_name": "Ada Lovelace",
            "title": "CTO",
            "profile_url": "https://linkedin.com/in/ada",
            "email": "ada@acme.dev",
            "company_id": COMPANY_ID,
            "buying_roles": ["technical_buyer"],
            "last_interaction_at": "2026-07-10",
        },
    )
    merged = set().union(*_admin_form_input_types(form_html).values())
    assert "url" in merged
    assert "email" in merged
    assert "date" in merged


@pytest.mark.unit
def test_research_forms_render_source_url_and_confidence_inputs() -> None:
    company_html = admin_research_pages.render_admin_company_research_page(
        company={"id": COMPANY_ID, "name": "Acme"},
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="operator",
    )
    company_types = set().union(*_admin_form_input_types(company_html).values())
    assert "url" in company_types
    assert "number" in company_types

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
    contact_types = set().union(*_admin_form_input_types(contact_html).values())
    assert "url" in contact_types
    assert "number" in contact_types


@pytest.mark.unit
@pytest.mark.integration
def test_preview_pipeline_detail_includes_themed_text_like_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "42")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    response = client.get(
        f"/admin/pipeline/{PREVIEW_PIPELINE_COMPANY_IDS[0]}",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert response.status_code == 200
    body = response.text
    assert 'class="admin-form"' in body
    assert 'type="datetime-local"' in body
    assert 'name="pipeline_owner"' in body
    assert 'name="expected_value_cents"' in body
    for attrs in _text_like_inputs_in_admin_forms(body):
        assert "style=" not in attrs


@pytest.mark.unit
def test_companies_list_archived_checkbox_renders_without_inline_styles() -> None:
    html = admin_companies.render_companies_list_page(
        admin_username="operator",
        csrf_token="csrf",
        filters={
            "q": "",
            "category": None,
            "stage": None,
            "target_status": None,
            "freshness": None,
            "archived": "1",
        },
        companies=[],
    )
    assert 'type="checkbox"' in html
    assert "Include archived" in html
    assert re.search(
        r'<label>\s*<input type="checkbox" name="archived"[^>]*/>\s*Include archived\s*</label>',
        html,
    )


@pytest.mark.unit
def test_contact_form_buying_role_checkboxes_use_admin_checkbox_class() -> None:
    html = admin_contacts.render_contact_form_page(
        csrf_token="csrf",
        admin_username="operator",
        companies=[{"id": COMPANY_ID, "name": "Acme"}],
        contact={
            "id": CONTACT_ID,
            "full_name": "Ada Lovelace",
            "buying_roles": ["technical_buyer"],
        },
    )
    assert 'class="admin-checkbox"' in html
    assert "<fieldset" in html


@pytest.mark.unit
def test_brief_convert_page_renders_themed_selection_markup() -> None:
    html = admin_pages.render_admin_brief_convert_page(
        admin_username="operator",
        brief={"id": PREVIEW_BRIEF_CONVERT_MATCHES_ID, "status": "paid"},
        back_filters=BriefListFilters(
            page=1,
            per_page=25,
            query=None,
            status=None,
            date_from=None,
            date_to=None,
            date_from_raw=None,
            date_to_raw=None,
        ),
        preview={
            "proposal": {
                "company_name": "Northwind",
                "pipeline_stage_label": "Diagnostic paid",
            },
            "company_matches": [
                {"id": COMPANY_ID, "name": "Northwind Existing", "domain": "northwind.dev"},
            ],
            "contact_matches": [
                {"id": CONTACT_ID, "email": "ops@northwind.dev"},
            ],
        },
        csrf_token="csrf",
    )
    assert 'class="brief-convert-fieldset"' in html
    assert 'class="brief-convert-choice"' in html
    assert 'class="brief-convert-match"' in html
    assert re.search(
        r'<label class="brief-convert-choice">\s*<input type="radio" name="company_choice"',
        html,
    )
    assert re.search(
        r'<label class="brief-convert-match">\s*<input type="radio" name="company_choice"',
        html,
    )


@pytest.mark.unit
@pytest.mark.integration
def test_preview_brief_convert_includes_native_selection_controls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "42")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    response = client.get(
        f"/admin/briefs/{PREVIEW_BRIEF_CONVERT_MATCHES_ID}/convert",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert response.status_code == 200
    body = response.text
    assert 'href="/assets/admin.css"' in body
    assert 'class="brief-convert-fieldset"' in body
    assert 'type="radio"' in body
