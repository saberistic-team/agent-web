"""Tests for the private admin dashboard shell (#102)."""

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
    assert ADMIN_LABELS == (
        "Dashboard",
        "Companies",
        "Contacts",
        "Signals",
        "Pipeline",
        "Imports",
        "Discovery",
        "Analytics",
        "Content",
        "Settings",
    )


@pytest.mark.unit
def test_render_admin_nav_marks_active_page() -> None:
    nav = render_admin_nav("/admin/companies")
    assert 'href="/admin/companies"' in nav
    assert 'aria-current="page"' in nav
    assert nav.count('aria-current="page"') == 1
    assert 'aria-label="Admin"' in nav


@pytest.mark.unit
def test_admin_requires_authentication() -> None:
    response = client.get("/admin")
    assert response.status_code == 401
    assert response.json()["detail"] == "Not authenticated"


@pytest.mark.unit
def test_admin_rejects_invalid_credentials() -> None:
    response = client.get("/admin", auth=("wrong", "credentials"))
    assert response.status_code == 401
    assert response.json()["detail"] == "Unauthorized"


@pytest.mark.unit
def test_admin_returns_503_when_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ADMIN_USERNAME", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    response = client.get("/admin", auth=ADMIN_AUTH)
    assert response.status_code == 503


@pytest.mark.unit
def test_admin_dashboard_renders_shell() -> None:
    response = client.get("/admin", auth=ADMIN_AUTH)
    assert response.status_code == 200
    body = response.text
    assert 'class="admin-app"' in body
    assert 'class="admin-layout"' in body
    assert 'class="admin-main"' in body
    assert 'id="main-content"' in body
    assert 'meta name="robots" content="noindex, nofollow"' in body
    assert 'href="/assets/admin.css"' in body
    assert "Admin foundation" in body


@pytest.mark.parametrize("path", ADMIN_HREFS)
@pytest.mark.unit
def test_admin_nav_links_present(path: str) -> None:
    response = client.get(path, auth=ADMIN_AUTH)
    assert response.status_code == 200
    body = response.text
    assert 'aria-label="Admin"' in body
    for href in ADMIN_HREFS:
        assert f'href="{href}"' in body
    for label in ADMIN_LABELS:
        assert label in body


@pytest.mark.parametrize(
    ("path", "label"),
    [
        ("/admin", "Dashboard"),
        ("/admin/companies", "Companies"),
        ("/admin/contacts", "Contacts"),
        ("/admin/signals", "Signals"),
        ("/admin/pipeline", "Pipeline"),
        ("/admin/imports", "Imports"),
        ("/admin/discovery", "Discovery"),
        ("/admin/analytics", "Analytics"),
        ("/admin/content", "Content"),
        ("/admin/settings", "Settings"),
    ],
)
@pytest.mark.unit
def test_admin_active_nav(path: str, label: str) -> None:
    response = client.get(path, auth=ADMIN_AUTH)
    assert response.status_code == 200
    body = response.text
    assert f'id="admin-empty-title">{label}</h1>' in body
    assert body.count('aria-current="page"') == 1
    assert f'href="{path}"' in body
    assert 'aria-current="page"' in body
    assert f'class="admin-nav-link" aria-current="page">{label}</a>' in body


@pytest.mark.parametrize(
    ("path", "milestone"),
    [
        ("/admin/companies", "CRM data model"),
        ("/admin/signals", "Signal intelligence"),
        ("/admin/content", "Content management"),
    ],
)
@pytest.mark.unit
def test_admin_empty_state_names_milestone(path: str, milestone: str) -> None:
    response = client.get(path, auth=ADMIN_AUTH)
    assert response.status_code == 200
    body = response.text
    assert milestone in body
    assert "will ship in the" in body
    assert "later issue" in body


@pytest.mark.unit
def test_admin_unknown_section_uses_admin_shell() -> None:
    response = client.get("/admin/unknown-section", auth=ADMIN_AUTH)
    assert response.status_code == 404
    body = response.text
    assert 'class="admin-app"' in body
    assert "Unknown admin page" in body
    assert "/admin/unknown-section" in body


@pytest.mark.unit
def test_admin_not_in_sitemap() -> None:
    response = client.get("/sitemap.xml")
    assert response.status_code == 200
    assert "/admin" not in response.text


@pytest.mark.unit
def test_robots_disallows_admin() -> None:
    response = client.get("/robots.txt")
    assert response.status_code == 200
    assert "Disallow: /admin" in response.text


@pytest.mark.unit
def test_public_home_unchanged() -> None:
    response = client.get("/")
    assert response.status_code == 200
    body = response.text
    assert 'class="top-nav"' in body
    assert 'aria-label="Primary"' in body
    assert 'class="admin-app"' not in body
    assert "/admin" not in body
