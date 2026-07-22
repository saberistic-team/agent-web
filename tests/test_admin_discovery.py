"""Tests for admin discovery run pages."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Generator
from unittest.mock import MagicMock, patch

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app import admin_auth, db
from app.admin_auth import SESSION_COOKIE_NAME
from app.admin_preview import PREVIEW_DISCOVERY_RUN_IDS, build_preview_discovery_run_detail
from app.config import get_settings
from app.main import app
from tests.conftest import enable_admin_preview_env

client = TestClient(app, follow_redirects=False)

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "test-session-secret-32chars-minimum"
TEST_LIMITER_SECRET = "test-limiter-secret-32chars-minimum!!"

_session_store: dict[str, dict[str, Any]] = {}


@pytest.fixture(autouse=True)
def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.setenv("DATABASE_URL", "postgresql://test/db")
    _session_store.clear()


@pytest.fixture
def authenticated_admin() -> Generator[dict[str, str], None, None]:
    raw_token = admin_auth.generate_session_token()
    token_hash = admin_auth.hash_session_token(raw_token)
    _session_store[token_hash] = {
        "id": 1,
        "token_hash": token_hash,
        "admin_username": TEST_USERNAME,
        "created_at": datetime.now(timezone.utc),
        "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
        "revoked_at": None,
        "csrf_token_hash": admin_auth.hash_csrf_token(
            admin_auth.derive_session_csrf_token(raw_token, get_settings())
        ),
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
        patch("app.admin_discovery_routes.db.db_connection", db_conn),
    ):
        db_conn.return_value.__enter__.return_value = mock_conn
        yield {SESSION_COOKIE_NAME: raw_token}


@pytest.mark.unit
def test_admin_discovery_list_requires_auth() -> None:
    response = client.get("/admin/discovery")
    assert response.status_code == 303


@pytest.mark.unit
def test_admin_discovery_list_preview(
    authenticated_admin: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enable_admin_preview_env(monkeypatch)
    response = client.get("/admin/discovery", cookies=authenticated_admin)
    assert response.status_code == 200
    assert "Discovery runs" in response.text
    assert "Run discovery now" in response.text
    assert "Preview data — not production" in response.text
    assert "ycombinator" in response.text


@pytest.mark.unit
def test_admin_discovery_detail_preview(
    authenticated_admin: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = PREVIEW_DISCOVERY_RUN_IDS[0]
    enable_admin_preview_env(monkeypatch)
    response = client.get(f"/admin/discovery/runs/{run_id}", cookies=authenticated_admin)
    assert response.status_code == 200
    assert "ycombinator" in response.text
    detail = build_preview_discovery_run_detail(str(run_id))
    assert detail is not None
    assert str(detail["sources"][0]["accepted_count"]) in response.text


@pytest.mark.unit
def test_admin_discovery_manual_run_redirects_to_detail(
    authenticated_admin: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from uuid import uuid4

    expected_run_id = uuid4()

    class _Result:
        run_id = expected_run_id
        status = "completed"
        lock_acquired = True
        message = None

    monkeypatch.setattr(
        "app.admin_discovery_routes.get_discovery_run_service",
        lambda: MagicMock(
            trigger_manual_run=MagicMock(return_value=_Result()),
        ),
    )
    csrf = admin_auth.derive_session_csrf_token(
        authenticated_admin[SESSION_COOKIE_NAME],
        get_settings(),
    )
    response = client.post(
        "/admin/discovery/run",
        cookies=authenticated_admin,
        data={"csrf_token": csrf},
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/admin/discovery/runs/{expected_run_id}"
