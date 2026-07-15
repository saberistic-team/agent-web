"""Tests for the private admin dashboard shell and contacts section."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Generator
from unittest.mock import MagicMock, patch

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app import admin_auth, db
from app.admin_auth import SESSION_COOKIE_NAME
from app.admin_layout import ADMIN_NAV_LINKS, render_admin_nav
from app.main import app

client = TestClient(app, follow_redirects=False)

ADMIN_HREFS = tuple(link["href"] for link in ADMIN_NAV_LINKS)
ADMIN_LABELS = tuple(link["label"] for link in ADMIN_NAV_LINKS)

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "test-session-secret-32chars-minimum"

_session_store: dict[str, dict[str, Any]] = {}


@pytest.fixture(autouse=True)
def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("DATABASE_URL", "postgresql://test/db")
    _session_store.clear()


@pytest.fixture
def authenticated_admin() -> Generator[dict[str, str], None, None]:
    raw_token = admin_auth.generate_session_token()
    csrf_raw = admin_auth.generate_csrf_value()
    token_hash = admin_auth.hash_session_token(raw_token)
    csrf_hash = admin_auth.hash_csrf_token(csrf_raw)
    _session_store[token_hash] = {
        "id": 1,
        "token_hash": token_hash,
        "admin_username": TEST_USERNAME,
        "created_at": datetime.now(timezone.utc),
        "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
        "revoked_at": None,
        "csrf_token_hash": csrf_hash,
    }

    def _get_session(conn: Any, th: str) -> dict[str, Any] | None:
        return _session_store.get(th)

    def _update_csrf(conn: Any, *, session_id: int, csrf_token_hash: str) -> None:
        for row in _session_store.values():
            if row["id"] == session_id:
                row["csrf_token_hash"] = csrf_token_hash

    mock_conn = MagicMock()
    with (
        patch.object(db, "get_admin_session_by_token_hash", side_effect=_get_session),
        patch.object(db, "update_admin_session_csrf", side_effect=_update_csrf),
        patch("app.db.db_connection") as db_conn,
        patch("app.admin_routes.db.db_connection", db_conn),
    ):
        db_conn.return_value.__enter__.return_value = mock_conn
        yield {SESSION_COOKIE_NAME: raw_token}


@pytest.mark.unit
@pytest.mark.integration
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
    assert "Contacts" in ADMIN_LABELS
    assert "Companies" in ADMIN_LABELS


@pytest.mark.unit
@pytest.mark.integration
def test_render_admin_nav_marks_active_page() -> None:
    nav = render_admin_nav("/admin/contacts")
    assert 'href="/admin/contacts"' in nav
    assert 'aria-current="page"' in nav
    assert nav.count('aria-current="page"') == 2
    assert 'aria-label="Admin"' in nav


@pytest.mark.unit
@pytest.mark.integration
def test_admin_requires_authentication() -> None:
    response = client.get("/admin")
    assert response.status_code == 303
    assert "/admin/login" in response.headers["location"]


@pytest.mark.unit
@pytest.mark.integration
def test_admin_returns_503_when_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ADMIN_USERNAME", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD_HASH", raising=False)
    monkeypatch.delenv("ADMIN_SESSION_SECRET", raising=False)
    response = client.get("/admin")
    assert response.status_code == 503


@pytest.mark.unit
@pytest.mark.integration
def test_admin_dashboard_renders_shell(authenticated_admin: dict[str, str]) -> None:
    response = client.get("/admin", cookies=authenticated_admin)
    assert response.status_code == 200
    body = response.text
    assert 'class="admin-app"' in body
    assert 'class="admin-layout"' in body
    assert 'meta name="robots" content="noindex, nofollow"' in body
    assert 'href="/assets/admin.css"' in body
    assert "Operations" in body


@pytest.mark.unit
@pytest.mark.integration
def test_admin_not_found(authenticated_admin: dict[str, str]) -> None:
    response = client.get("/admin/unknown-section", cookies=authenticated_admin)
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
def test_admin_placeholder_sections_still_render(authenticated_admin: dict[str, str]) -> None:
    response = client.get("/admin/signals", cookies=authenticated_admin)
    assert response.status_code == 200
    assert "Signals" in response.text
    assert "Signal intelligence" in response.text
