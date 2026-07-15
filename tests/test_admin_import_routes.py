"""Tests for LinkedIn import admin routes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Generator
from unittest.mock import MagicMock, patch

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app import admin_auth, db
from app.admin_auth import SESSION_COOKIE_NAME
from app.main import app

client = TestClient(app, follow_redirects=False)

TEST_USERNAME = "operator"
TEST_HASH = PasswordHasher().hash("correct-horse-battery-staple")
TEST_SECRET = "test-session-secret-32chars-minimum"

_session_store: dict[str, dict[str, Any]] = {}


@pytest.fixture(autouse=True)
def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("DATABASE_URL", "postgresql://test/db")
    monkeypatch.setenv("BASE_URL", "http://testserver")
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
def test_imports_requires_authentication() -> None:
    response = client.get("/admin/imports")
    assert response.status_code == 303
    assert "/admin/login" in response.headers["location"]


@pytest.mark.unit
@pytest.mark.integration
def test_imports_page_renders_upload_ui(authenticated_admin: dict[str, str]) -> None:
    response = client.get("/admin/imports", cookies=authenticated_admin)
    assert response.status_code == 200
    body = response.text
    assert 'id="imports-title"' in body
    assert "Imports" in body
    assert 'id="linkedin-export-file"' in body
    assert 'accept=".zip,application/zip"' in body
    assert "/assets/linkedin-export.js" in body
    assert "/assets/fflate.min.js" in body
    assert "Privacy and data handling" in body
    assert "Nothing in the default flow" in body
    assert "Parsing stays on this device" in body
    assert 'enctype="multipart/form-data"' not in body
    assert 'action="/admin/imports"' not in body


@pytest.mark.unit
@pytest.mark.integration
def test_imports_preview_mode_shows_mock_data(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "109")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    response = client.get("/admin/imports")
    assert response.status_code == 200
    body = response.text
    assert "Preview data — not production" in body
    assert "Recognized files" in body
    assert "Connections.csv" in body
    assert "Proposed import preview" in body
    assert "/assets/linkedin-export.js" not in body


@pytest.mark.unit
def test_imports_preview_builder_seed_stable() -> None:
    import random

    from app.admin_preview import build_preview_linkedin_import

    a = build_preview_linkedin_import(rng=random.Random(109))
    b = build_preview_linkedin_import(rng=random.Random(109))
    assert a == b
    assert a["counts"]["connections"] >= 120
