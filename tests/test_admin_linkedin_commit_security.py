"""CSRF and JSON request guards for POST /admin/api/imports/linkedin/commit (#329)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Generator
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app import admin_auth, db
from app.admin_auth import SESSION_COOKIE_NAME
from app.admin_linkedin_commit import LINKEDIN_COMMIT_MAX_BODY_BYTES, LINKEDIN_COMMIT_MAX_CONNECTIONS
from app.config import get_settings
from app.main import app
from tests.conftest import enable_admin_preview_env
from tests.test_admin_import_batches import (
    TEST_HASH,
    TEST_LIMITER_SECRET,
    TEST_SECRET,
    TEST_USERNAME,
    _linkedin_commit_headers,
    _session_csrf_for_cookies,
    _session_store,
)

client = TestClient(app, follow_redirects=False)

_VALID_CONNECTION = {
    "profile_url": "https://linkedin.com/in/ada-lovelace",
    "full_name": "Ada Lovelace",
}


@pytest.fixture(autouse=True)
def admin_env(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.setenv("DATABASE_URL", "postgresql://test/db")
    _session_store.clear()

    def _get_session(conn: Any, th: str) -> dict[str, Any] | None:
        return _session_store.get(th)

    mock_conn = MagicMock()
    with (
        patch.object(db, "get_admin_session_by_token_hash", side_effect=_get_session),
        patch("app.db.db_connection") as db_conn,
        patch("app.admin_routes.db.db_connection", db_conn),
    ):
        db_conn.return_value.__enter__.return_value = mock_conn
        yield


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
    yield {SESSION_COOKIE_NAME: raw_token}


def _seed_second_session(*, session_id: int = 2) -> tuple[dict[str, str], str]:
    raw_token = admin_auth.generate_session_token()
    token_hash = admin_auth.hash_session_token(raw_token)
    _session_store[token_hash] = {
        "id": session_id,
        "token_hash": token_hash,
        "admin_username": TEST_USERNAME,
        "created_at": datetime.now(timezone.utc),
        "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
        "revoked_at": None,
        "csrf_token_hash": None,
    }
    cookies = {SESSION_COOKIE_NAME: raw_token}
    return cookies, admin_auth.derive_session_csrf_token(raw_token, get_settings())


def _commit(
    *,
    cookies: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    json_body: dict[str, Any] | list[Any] | None = None,
    content: bytes | None = None,
    content_type: str | None = "application/json",
) -> Any:
    request_headers = dict(headers or {})
    if content_type is not None and "Content-Type" not in request_headers:
        request_headers["Content-Type"] = content_type
    if json_body is not None:
        return client.post(
            "/admin/api/imports/linkedin/commit",
            cookies=cookies,
            headers=request_headers,
            json=json_body,
        )
    return client.post(
        "/admin/api/imports/linkedin/commit",
        cookies=cookies,
        headers=request_headers,
        content=content if content is not None else b"",
    )


@pytest.mark.unit
@pytest.mark.integration
def test_linkedin_commit_anonymous_redirects_before_service() -> None:
    with patch("app.admin_routes._crm.commit_linkedin_import") as commit:
        response = _commit(json_body={"connections": [_VALID_CONNECTION]})
    assert response.status_code == 303
    assert response.headers["location"].startswith("/admin/login")
    commit.assert_not_called()


@pytest.mark.unit
@pytest.mark.integration
def test_linkedin_commit_missing_csrf_rejects_without_service(
    authenticated_admin: dict[str, str],
) -> None:
    with patch("app.admin_routes._crm.commit_linkedin_import") as commit:
        response = _commit(
            cookies=authenticated_admin,
            headers={"Content-Type": "application/json"},
            json_body={"connections": [_VALID_CONNECTION]},
        )
    assert response.status_code == 400
    assert admin_auth.INVALID_REQUEST_MESSAGE in response.json()["detail"]
    commit.assert_not_called()


@pytest.mark.unit
@pytest.mark.integration
def test_linkedin_commit_rejects_cross_session_csrf_without_service(
    authenticated_admin: dict[str, str],
) -> None:
    session_b, _csrf_b = _seed_second_session()
    csrf_from_a = _session_csrf_for_cookies(authenticated_admin)
    with patch("app.admin_routes._crm.commit_linkedin_import") as commit:
        response = _commit(
            cookies=session_b,
            headers={
                admin_auth.CSRF_HEADER_NAME: csrf_from_a,
                "Content-Type": "application/json",
            },
            json_body={"connections": [_VALID_CONNECTION]},
        )
    assert response.status_code == 400
    assert admin_auth.INVALID_REQUEST_MESSAGE in response.json()["detail"]
    commit.assert_not_called()


@pytest.mark.unit
@pytest.mark.integration
def test_linkedin_commit_rejects_revoked_session_without_service() -> None:
    cookies, csrf = _seed_second_session(session_id=3)
    token_hash = admin_auth.hash_session_token(cookies[SESSION_COOKIE_NAME])
    _session_store[token_hash]["revoked_at"] = datetime.now(timezone.utc)
    with patch("app.admin_routes._crm.commit_linkedin_import") as commit:
        response = _commit(
            cookies=cookies,
            headers={
                admin_auth.CSRF_HEADER_NAME: csrf,
                "Content-Type": "application/json",
            },
            json_body={"connections": [_VALID_CONNECTION]},
        )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/admin/login")
    commit.assert_not_called()


@pytest.mark.unit
@pytest.mark.integration
def test_linkedin_commit_rejects_expired_session_without_service() -> None:
    cookies, csrf = _seed_second_session(session_id=4)
    token_hash = admin_auth.hash_session_token(cookies[SESSION_COOKIE_NAME])
    _session_store[token_hash]["expires_at"] = datetime.now(timezone.utc) - timedelta(minutes=1)
    with patch("app.admin_routes._crm.commit_linkedin_import") as commit:
        response = _commit(
            cookies=cookies,
            headers={
                admin_auth.CSRF_HEADER_NAME: csrf,
                "Content-Type": "application/json",
            },
            json_body={"connections": [_VALID_CONNECTION]},
        )
    assert response.status_code == 303
    commit.assert_not_called()


@pytest.mark.unit
@pytest.mark.integration
@pytest.mark.parametrize(
    "csrf_token",
    [
        "",
        "x" * (admin_auth.LOGIN_CSRF_MAX_LENGTH + 1),
        "not-a-valid-session-token",
    ],
)
def test_linkedin_commit_rejects_malformed_csrf_generically(
    authenticated_admin: dict[str, str],
    csrf_token: str,
) -> None:
    with patch("app.admin_routes._crm.commit_linkedin_import") as commit:
        response = _commit(
            cookies=authenticated_admin,
            headers={
                admin_auth.CSRF_HEADER_NAME: csrf_token,
                "Content-Type": "application/json",
            },
            json_body={"connections": [_VALID_CONNECTION]},
        )
    assert response.status_code == 400
    assert admin_auth.INVALID_REQUEST_MESSAGE in response.json()["detail"]
    commit.assert_not_called()


@pytest.mark.unit
@pytest.mark.integration
def test_linkedin_commit_rejects_duplicate_csrf_transports(
    authenticated_admin: dict[str, str],
) -> None:
    headers = _linkedin_commit_headers(authenticated_admin)
    with patch("app.admin_routes._crm.commit_linkedin_import") as commit:
        response = _commit(
            cookies=authenticated_admin,
            headers=headers,
            json_body={
                "csrf_token": headers[admin_auth.CSRF_HEADER_NAME],
                "connections": [_VALID_CONNECTION],
            },
        )
    assert response.status_code == 400
    assert admin_auth.INVALID_REQUEST_MESSAGE in response.json()["detail"]
    commit.assert_not_called()


@pytest.mark.unit
@pytest.mark.integration
@pytest.mark.parametrize(
    "content_type,body",
    [
        ("text/plain", b'{"connections":[]}'),
        ("application/x-www-form-urlencoded", b"connections=x"),
        (
            "multipart/form-data; boundary=----csrf",
            b"------csrf\r\nContent-Disposition: form-data; name=\"connections\"\r\n\r\n[]\r\n------csrf--\r\n",
        ),
        (None, b'{"connections":[]}'),
        ("application/json", b"["),
        ("application/json", b"[]"),
        ("application/json", b"x" * (LINKEDIN_COMMIT_MAX_BODY_BYTES + 1)),
    ],
)
def test_linkedin_commit_rejects_bad_request_formats_before_service(
    authenticated_admin: dict[str, str],
    content_type: str | None,
    body: bytes,
) -> None:
    headers = _linkedin_commit_headers(authenticated_admin)
    if content_type is None:
        headers.pop("Content-Type", None)
    else:
        headers["Content-Type"] = content_type
    with patch("app.admin_routes._crm.commit_linkedin_import") as commit:
        response = _commit(cookies=authenticated_admin, headers=headers, content=body, content_type=None)
    assert response.status_code in {400, 413, 415}
    commit.assert_not_called()


@pytest.mark.unit
@pytest.mark.integration
def test_linkedin_commit_rejects_oversized_connections_list(
    authenticated_admin: dict[str, str],
) -> None:
    oversized = [_VALID_CONNECTION] * (LINKEDIN_COMMIT_MAX_CONNECTIONS + 1)
    with patch("app.admin_routes._crm.commit_linkedin_import") as commit:
        response = _commit(
            cookies=authenticated_admin,
            headers=_linkedin_commit_headers(authenticated_admin),
            json_body={"connections": oversized},
        )
    assert response.status_code == 400
    assert "connections list too large" in response.json()["detail"]
    commit.assert_not_called()


@pytest.mark.unit
@pytest.mark.integration
def test_linkedin_commit_valid_request_persists_once(
    authenticated_admin: dict[str, str],
) -> None:
    with patch("app.admin_routes._crm.commit_linkedin_import") as commit:
        commit.return_value = {
            "batch": {
                "id": "11111111-1111-1111-1111-111111111111",
                "status": "committed",
                "checksum": "abc123",
            },
            "idempotent": False,
            "summary_counts": {
                "inserted": 1,
                "updated": 0,
                "unchanged": 0,
                "skipped": 0,
                "conflicted": 0,
            },
        }
        response = _commit(
            cookies=authenticated_admin,
            headers=_linkedin_commit_headers(authenticated_admin),
            json_body={
                "export_date": "2026-01-15",
                "connections": [_VALID_CONNECTION],
                "checksum": "abc123",
            },
        )
    assert response.status_code == 200
    payload = response.json()
    assert payload["batch_id"] == "11111111-1111-1111-1111-111111111111"
    assert payload["idempotent"] is False
    assert payload["summary_counts"]["inserted"] == 1
    commit.assert_called_once()


@pytest.mark.unit
@pytest.mark.integration
def test_linkedin_commit_rejections_have_no_side_effects(
    authenticated_admin: dict[str, str],
) -> None:
    audit_called = False

    def _forbidden_audit(*args: Any, **kwargs: Any) -> None:
        nonlocal audit_called
        audit_called = True

    with (
        patch("app.admin_routes._crm.commit_linkedin_import") as commit,
        patch("app.admin_routes.audit_service.record_import_batch", side_effect=_forbidden_audit),
    ):
        response = _commit(
            cookies=authenticated_admin,
            headers={"Content-Type": "application/json"},
            json_body={"connections": [_VALID_CONNECTION]},
        )
    assert response.status_code == 400
    commit.assert_not_called()
    assert audit_called is False


@pytest.mark.unit
@pytest.mark.integration
def test_linkedin_commit_preview_mode_stays_read_only(
    authenticated_admin: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enable_admin_preview_env(monkeypatch)
    with patch("app.admin_routes._crm.commit_linkedin_import") as commit:
        response = _commit(
            cookies=authenticated_admin,
            headers=_linkedin_commit_headers(authenticated_admin),
            json_body={"connections": [_VALID_CONNECTION]},
        )
    assert response.status_code == 405
    assert response.headers.get("allow") == "GET, HEAD"
    commit.assert_not_called()


@pytest.mark.unit
@pytest.mark.integration
def test_text_plain_json_cannot_reach_commit_service_even_with_csrf(
    authenticated_admin: dict[str, str],
) -> None:
    body = json.dumps({"connections": [_VALID_CONNECTION]}).encode()
    headers = _linkedin_commit_headers(authenticated_admin)
    headers["Content-Type"] = "text/plain"
    with patch("app.admin_routes._crm.commit_linkedin_import") as commit:
        response = _commit(
            cookies=authenticated_admin,
            headers=headers,
            content=body,
            content_type=None,
        )
    assert response.status_code == 415
    commit.assert_not_called()
