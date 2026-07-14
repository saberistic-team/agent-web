"""Tests for the private admin dashboard shell and contacts section."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.admin_layout import ADMIN_NAV_LINKS, render_admin_nav
from app.main import app

client = TestClient(app)

ADMIN_HREFS = tuple(link["href"] for link in ADMIN_NAV_LINKS)
ADMIN_LABELS = tuple(link["label"] for link in ADMIN_NAV_LINKS)
ADMIN_AUTH = ("admin", "test-pass")


@pytest.fixture(autouse=True)
def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_USERNAME", ADMIN_AUTH[0])
    monkeypatch.setenv("ADMIN_PASSWORD", ADMIN_AUTH[1])


@pytest.mark.unit
@pytest.mark.integration
def test_admin_nav_links_include_required_destinations() -> None:
    assert ADMIN_HREFS == (
        "/admin",
        "/admin/companies",
        "/admin/contacts",
        "/admin/signals",
        "/admin/pipeline",
        "/admin/imports",
        "/admin/discovery",
        "/admin/analytics",
        "/admin/content",
        "/admin/settings",
    )
    assert "Contacts" in ADMIN_LABELS
    assert "Companies" in ADMIN_LABELS


@pytest.mark.unit
@pytest.mark.integration
def test_render_admin_nav_marks_active_page() -> None:
    nav = render_admin_nav("/admin/contacts")
    assert 'href="/admin/contacts"' in nav
    assert 'aria-current="page"' in nav
    assert nav.count('aria-current="page"') == 1
    assert 'aria-label="Admin"' in nav


@pytest.mark.unit
@pytest.mark.integration
def test_admin_requires_authentication() -> None:
    response = client.get("/admin")
    assert response.status_code == 401


@pytest.mark.unit
@pytest.mark.integration
def test_admin_rejects_invalid_credentials() -> None:
    response = client.get("/admin", auth=("wrong", "credentials"))
    assert response.status_code == 401


@pytest.mark.unit
@pytest.mark.integration
def test_admin_returns_503_when_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ADMIN_USERNAME", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    response = client.get("/admin", auth=ADMIN_AUTH)
    assert response.status_code == 503


@pytest.mark.unit
@pytest.mark.integration
def test_admin_dashboard_renders_shell() -> None:
    response = client.get("/admin", auth=ADMIN_AUTH)
    assert response.status_code == 200
    body = response.text
    assert 'class="admin-app"' in body
    assert 'class="admin-layout"' in body
    assert 'meta name="robots" content="noindex, nofollow"' in body
    assert 'href="/assets/admin.css"' in body
    assert "Operations" in body


@pytest.mark.unit
@pytest.mark.integration
def test_admin_not_found() -> None:
    response = client.get("/admin/unknown-section", auth=ADMIN_AUTH)
    assert response.status_code == 404
    assert "Unknown admin page" in response.text


@pytest.mark.unit
@pytest.mark.integration
def test_admin_render_helpers() -> None:
    from app import admin

    page = admin.render_admin_page("/admin/signals")
    assert "Signals" in page
    assert admin.is_admin_path("/admin/contacts")
    assert not admin.is_admin_path("/admin/contacts/new")
    not_found = admin.render_admin_not_found("/admin/missing")
    assert "Unknown admin page" in not_found


@pytest.mark.unit
@pytest.mark.integration
def test_admin_placeholder_sections_still_render() -> None:
    response = client.get("/admin/signals", auth=ADMIN_AUTH)
    assert response.status_code == 200
    assert "Signals" in response.text
    assert "Signal intelligence" in response.text
