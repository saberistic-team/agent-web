"""Regression tests for dark-themed admin native selection and date controls (#235)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app import admin_companies, admin_contacts, admin_pages
from app.admin_auth import SESSION_COOKIE_NAME
from app.admin_pipeline_pages import render_pipeline_detail_page
from app.admin_preview import PREVIEW_PIPELINE_COMPANY_IDS
from app.admin_routes import PREVIEW_SESSION_TOKEN
from app.brief_service import BriefListFilters
from app.main import app

ADMIN_CSS = Path(__file__).resolve().parents[1] / "site/assets/admin.css"
SITE_CSS = Path(__file__).resolve().parents[1] / "site/assets/site.css"

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "test-session-secret-32chars-minimum"

PREVIEW_COMPANY_MATCH_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"
PREVIEW_CONTACT_MATCH_ID = "dddddddd-dddd-dddd-dddd-dddddddddddd"

client = TestClient(app, follow_redirects=False)


@pytest.fixture(autouse=True)
def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")


def _admin_css() -> str:
    return ADMIN_CSS.read_text(encoding="utf-8")


def _site_css() -> str:
    return SITE_CSS.read_text(encoding="utf-8")


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


def _preview_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("BASE_URL", "http://127.0.0.1:8765")
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "42")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    return client


def _preview_csrf() -> str:
    from app import admin_auth
    from app.config import get_settings

    return admin_auth.derive_session_csrf_token(PREVIEW_SESSION_TOKEN, get_settings())


@pytest.mark.unit
def test_admin_app_declares_dark_color_scheme_and_accent() -> None:
    css = _admin_css()
    block = _rule_block(css, "body.admin-app {")
    assert "color-scheme: dark" in block
    assert "accent-color: var(--accent)" in block


@pytest.mark.unit
def test_admin_checkbox_radio_use_accent_color() -> None:
    css = _admin_css()
    block = _rule_block(css, '.admin-form input[type="checkbox"],')
    assert "accent-color: var(--accent)" in block
    assert '.admin-form input[type="radio"]' in block
    assert "cursor: pointer" in block


@pytest.mark.unit
def test_admin_checkbox_radio_focus_and_disabled_states() -> None:
    css = _admin_css()
    focus_block = _rule_block(css, '.admin-form input[type="checkbox"]:focus-visible,')
    assert "outline: 2px solid var(--accent)" in focus_block
    assert '.admin-form input[type="radio"]:focus-visible' in focus_block

    disabled_block = _rule_block(css, '.admin-form input[type="checkbox"]:disabled,')
    assert "opacity: 0.65" in disabled_block
    assert "cursor: not-allowed" in disabled_block


@pytest.mark.unit
def test_brief_convert_fieldset_and_choice_styles() -> None:
    css = _admin_css()
    fieldset_block = _rule_block(css, ".brief-convert-fieldset {")
    assert "border: 1px solid var(--line)" in fieldset_block
    assert "background: color-mix(in srgb, var(--surface) 55%, transparent)" in fieldset_block

    choice_block = _rule_block(css, ".brief-convert-choice,")
    assert "cursor: pointer" in choice_block
    assert ".brief-convert-match" in choice_block
    assert ":has(input:checked)" in css
    assert ":has(input:focus-visible)" in css


@pytest.mark.unit
def test_admin_checkbox_label_and_fieldset_styles() -> None:
    css = _admin_css()
    assert ".admin-checkbox {" in css
    checkbox_block = _rule_block(css, ".admin-checkbox {")
    assert "display: flex" in checkbox_block
    assert "cursor: pointer" in checkbox_block

    fieldset_block = _rule_block(css, ".admin-form fieldset.field,")
    assert "border: 1px solid var(--line)" in fieldset_block
    assert ".brief-convert-fieldset" in fieldset_block


@pytest.mark.unit
def test_brief_filter_and_admin_date_controls_use_dark_color_scheme() -> None:
    css = _admin_css()
    block = _rule_block(css, '.admin-form input[type="date"],')
    assert "color-scheme: dark" in block
    assert '.brief-filter input[type="date"]' in block
    assert "::-webkit-calendar-picker-indicator" in css


@pytest.mark.unit
def test_admin_css_asset_served_for_admin_shell() -> None:
    response = client.get("/assets/admin.css")
    assert response.status_code == 200
    body = response.text
    assert "color-scheme: dark" in body
    assert ".brief-convert-fieldset" in body
    assert "accent-color: var(--accent)" in body


@pytest.mark.unit
def test_public_site_css_does_not_declare_admin_color_scheme() -> None:
    css = _site_css()
    assert "color-scheme:" not in css
    assert "accent-color:" not in css

    home = client.get("/")
    assert home.status_code == 200
    assert 'href="/assets/admin.css"' not in home.text


@pytest.mark.unit
def test_companies_list_renders_themed_archived_checkbox() -> None:
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
    assert 'href="/assets/admin.css"' in html
    assert 'name="archived"' in html
    assert 'type="checkbox"' in html
    assert " checked" in html
    assert 'class="brief-convert-fieldset"' not in html


@pytest.mark.unit
def test_contact_form_renders_themed_checkbox_group_and_date() -> None:
    from uuid import UUID

    company_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    contact_id = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
    html = admin_contacts.render_contact_form_page(
        csrf_token="csrf",
        admin_username="operator",
        companies=[{"id": company_id, "name": "Acme"}],
        contact={
            "id": contact_id,
            "full_name": "Ada Lovelace",
            "title": "CTO",
            "profile_url": "https://linkedin.com/in/ada",
            "email": "ada@acme.dev",
            "company_id": company_id,
            "buying_roles": ["technical_buyer"],
            "last_interaction_at": "2026-07-10",
        },
    )
    assert 'class="admin-checkbox"' in html
    assert 'type="date"' in html
    assert "<fieldset" in html
    assert 'name="buying_roles"' in html


@pytest.mark.unit
def test_pipeline_detail_renders_themed_checkbox_and_datetime() -> None:
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
    assert 'type="datetime-local"' in html
    assert 'name="confirm"' in html
    assert 'type="checkbox"' in html
    assert 'class="admin-form admin-form--editor"' in html


@pytest.mark.unit
def test_brief_convert_renders_native_radios_inside_labels() -> None:
    html = admin_pages.render_admin_brief_convert_page(
        admin_username="operator",
        brief={"id": 4, "status": "paid"},
        back_filters=BriefListFilters(
            page=1,
            per_page=20,
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
                "website": "https://northwind.example",
                "domain": "northwind.example",
                "contact_email": "ops@northwind.example",
                "pipeline_stage_label": "Diagnostic paid",
                "brief_status": "paid",
                "expected_value": 200.0,
            },
            "company_matches": [
                {
                    "id": PREVIEW_COMPANY_MATCH_ID,
                    "name": "Northwind Labs (existing)",
                    "domain": "northwind.example",
                }
            ],
            "contact_matches": [],
        },
        csrf_token="csrf",
    )
    assert 'class="brief-convert-fieldset"' in html
    assert 'class="brief-convert-choice"' in html
    assert 'class="brief-convert-match"' in html
    assert re.search(
        r'<label class="brief-convert-match">\s*<input type="radio" name="company_choice"',
        html,
    )
    assert re.search(
        r'<label class="brief-convert-choice">\s*<input type="radio" name="contact_choice"',
        html,
    )


@pytest.mark.unit
@pytest.mark.integration
def test_preview_briefs_page_includes_date_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _preview_client(monkeypatch)
    response = client.get(
        "/admin/briefs",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert response.status_code == 200
    body = response.text
    assert 'type="date"' in body
    assert 'name="date_from"' in body
    assert 'class="brief-filter"' in body


@pytest.mark.unit
@pytest.mark.integration
def test_preview_convert_page_includes_themed_native_controls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _preview_client(monkeypatch)
    response = client.get(
        "/admin/briefs/4/convert",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert response.status_code == 200
    body = response.text
    assert 'href="/assets/admin.css"' in body
    assert 'class="brief-convert-fieldset"' in body
    assert 'type="radio"' in body
    assert "Northwind Labs (existing)" in body


@pytest.mark.unit
@pytest.mark.integration
def test_preview_pipeline_detail_includes_native_controls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _preview_client(monkeypatch)
    response = client.get(
        f"/admin/pipeline/{PREVIEW_PIPELINE_COMPANY_IDS[0]}",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert response.status_code == 200
    body = response.text
    assert 'type="datetime-local"' in body
    assert 'name="confirm"' in body
    assert 'type="checkbox"' in body


@pytest.mark.unit
@pytest.mark.integration
def test_preview_companies_page_includes_archived_checkbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _preview_client(monkeypatch)
    response = client.get(
        "/admin/companies",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert response.status_code == 200
    body = response.text
    assert 'class="admin-form admin-form--compact"' in body
    assert 'name="archived"' in body
    assert 'type="checkbox"' in body
    assert "Include archived" in body
    assert "No companies match these filters." not in body


@pytest.mark.unit
@pytest.mark.integration
def test_preview_contacts_page_includes_archived_checkbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _preview_client(monkeypatch)
    response = client.get(
        "/admin/contacts",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert response.status_code == 200
    body = response.text
    assert 'class="admin-form admin-form--compact"' in body
    assert 'name="archived"' in body
    assert 'type="checkbox"' in body
    assert "Include archived" in body
    assert "No contacts match these filters." not in body


@pytest.mark.unit
def test_contacts_list_renders_themed_archived_checkbox() -> None:
    from uuid import UUID

    company_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    html = admin_contacts.render_contacts_list_page(
        admin_username="operator",
        csrf_token="csrf",
        filters={
            "q": "",
            "company_id": None,
            "buying_role": None,
            "archived": "1",
        },
        contacts=[],
        companies=[{"id": company_id, "name": "Acme"}],
    )
    assert 'href="/assets/admin.css"' in html
    assert 'name="archived"' in html
    assert 'type="checkbox"' in html
    assert " checked" in html


@pytest.mark.unit
@pytest.mark.integration
def test_preview_convert_existing_company_radio_post_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _preview_client(monkeypatch)
    response = client.post(
        "/admin/briefs/4/convert",
        data={
            "csrf_token": _preview_csrf(),
            "company_choice": f"existing:{PREVIEW_COMPANY_MATCH_ID}",
            "contact_choice": "new",
        },
        cookies={SESSION_COOKIE_NAME: PREVIEW_SESSION_TOKEN},
    )
    assert response.status_code == 405
    assert response.headers.get("allow") == "GET, HEAD"


@pytest.mark.unit
@pytest.mark.integration
def test_preview_convert_keyboard_existing_contact_radio_post_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unsafe preview POST is denied centrally before route handlers run."""
    _preview_client(monkeypatch)
    response = client.post(
        "/admin/briefs/4/convert",
        data={
            "csrf_token": _preview_csrf(),
            "company_choice": "new",
            "contact_choice": f"existing:{PREVIEW_CONTACT_MATCH_ID}",
        },
        cookies={SESSION_COOKIE_NAME: PREVIEW_SESSION_TOKEN},
    )
    assert response.status_code == 405
    assert response.headers.get("allow") == "GET, HEAD"
