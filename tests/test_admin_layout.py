"""Tests for the private admin dashboard shell (#102)."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Generator
from unittest.mock import MagicMock, patch

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app.admin_auth import SESSION_COOKIE_NAME
from app.admin_layout import ADMIN_NAV_LINKS, render_admin_nav
from app.main import app

client = TestClient(app, follow_redirects=False)

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "test-session-secret-32chars-minimum"

ADMIN_HREFS = tuple(link["href"] for link in ADMIN_NAV_LINKS)
ADMIN_LABELS = tuple(link["label"] for link in ADMIN_NAV_LINKS)


@pytest.fixture(autouse=True)
def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")


@contextmanager
def mock_db_connection() -> Generator[MagicMock, None, None]:
    conn = MagicMock()
    with patch("app.admin_routes.db.db_connection") as db_conn:
        db_conn.return_value.__enter__.return_value = conn
        db_conn.return_value.__exit__.return_value = None
        yield conn


def _session_row(*, token_hash: str) -> dict[str, Any]:
    return {
        "id": 1,
        "token_hash": token_hash,
        "admin_username": TEST_USERNAME,
        "created_at": datetime.now(timezone.utc),
        "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
        "revoked_at": None,
    }


@pytest.mark.unit
def test_admin_nav_links_include_required_destinations() -> None:
    assert ADMIN_HREFS == (
        "/admin",
        "/admin/audit",
        "/admin/briefs",
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
        "Audit",
        "Briefs",
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
@pytest.mark.integration
def test_anonymous_admin_dashboard_redirects_to_login() -> None:
    response = client.get("/admin")
    assert response.status_code == 303
    assert response.headers["location"].startswith("/admin/login?next=")


@pytest.mark.unit
@pytest.mark.integration
def test_anonymous_admin_section_redirects_to_login() -> None:
    response = client.get("/admin/companies")
    assert response.status_code == 303
    assert "/admin/login" in response.headers["location"]


@pytest.mark.unit
@pytest.mark.integration
def test_admin_dashboard_renders_shell() -> None:
    from app import admin_auth

    raw_token = admin_auth.generate_session_token()
    token_hash = admin_auth.hash_session_token(raw_token)
    row = _session_row(token_hash=token_hash)
    with mock_db_connection():
        with patch(
            "app.admin_routes.db.get_admin_session_by_token_hash",
            return_value=row,
        ):
            response = client.get("/admin", cookies={SESSION_COOKIE_NAME: raw_token})
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
@pytest.mark.integration
def test_admin_nav_links_present(path: str) -> None:
    from app import admin_auth

    raw_token = admin_auth.generate_session_token()
    token_hash = admin_auth.hash_session_token(raw_token)
    row = _session_row(token_hash=token_hash)
    with mock_db_connection():
        with patch(
            "app.admin_routes.db.get_admin_session_by_token_hash",
            return_value=row,
        ):
            response = client.get(path, cookies={SESSION_COOKIE_NAME: raw_token})
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
@pytest.mark.integration
def test_admin_active_nav(path: str, label: str) -> None:
    from app import admin_auth

    raw_token = admin_auth.generate_session_token()
    token_hash = admin_auth.hash_session_token(raw_token)
    row = _session_row(token_hash=token_hash)
    with mock_db_connection():
        with patch(
            "app.admin_routes.db.get_admin_session_by_token_hash",
            return_value=row,
        ):
            response = client.get(path, cookies={SESSION_COOKIE_NAME: raw_token})
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
@pytest.mark.integration
def test_admin_empty_state_names_milestone(path: str, milestone: str) -> None:
    from app import admin_auth

    raw_token = admin_auth.generate_session_token()
    token_hash = admin_auth.hash_session_token(raw_token)
    row = _session_row(token_hash=token_hash)
    with mock_db_connection():
        with patch(
            "app.admin_routes.db.get_admin_session_by_token_hash",
            return_value=row,
        ):
            response = client.get(path, cookies={SESSION_COOKIE_NAME: raw_token})
    assert response.status_code == 200
    body = response.text
    assert milestone in body
    assert "will ship in the" in body
    assert "later issue" in body


@pytest.mark.unit
@pytest.mark.integration
def test_admin_unknown_section_uses_admin_shell() -> None:
    from app import admin_auth

    raw_token = admin_auth.generate_session_token()
    token_hash = admin_auth.hash_session_token(raw_token)
    row = _session_row(token_hash=token_hash)
    with mock_db_connection():
        with patch(
            "app.admin_routes.db.get_admin_session_by_token_hash",
            return_value=row,
        ):
            response = client.get(
                "/admin/unknown-section",
                cookies={SESSION_COOKIE_NAME: raw_token},
            )
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


@pytest.mark.unit
@pytest.mark.integration
def test_admin_preview_mode_accepts_preview_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    response = client.get(
        "/admin",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert response.status_code == 200
    assert 'class="admin-app"' in response.text


@pytest.mark.unit
@pytest.mark.integration
def test_admin_preview_mode_renders_section_mock_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "42")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    response = client.get("/admin/companies")
    assert response.status_code == 200
    assert "Preview data — not production" in response.text
    assert "admin-table" in response.text
    assert "Companies" in response.text
