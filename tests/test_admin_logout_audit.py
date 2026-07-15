"""Tests for admin logout audit policy (#217)."""

from __future__ import annotations

import copy
import os
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Generator
from unittest.mock import MagicMock, patch

import psycopg
import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from app import admin_auth, audit_service, db
from app.admin_auth import SESSION_COOKIE_NAME
from app.config import get_settings
from app.crm_uow import crm_transaction
from app.main import app
from app.migrations.runner import apply_migrations

client = TestClient(app, follow_redirects=False)

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "test-session-secret-32chars-minimum"
TEST_LIMITER_SECRET = "test-limiter-secret-32chars-minimum!"

_REQUIRED = (os.environ.get("REQUIRE_TEST_DATABASE") or "").strip() in {"1", "true", "yes"}
_DATABASE_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip()

_session_store: dict[str, dict[str, Any]] = {}
_session_store_lock = threading.Lock()


@pytest.fixture(autouse=True)
def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    admin_auth.reset_login_rate_limiter()
    _session_store.clear()


def _require_database_url() -> str:
    if _DATABASE_URL:
        return _DATABASE_URL
    if _REQUIRED:
        pytest.fail("REQUIRE_TEST_DATABASE=1 but TEST_DATABASE_URL is unset")
    pytest.skip("TEST_DATABASE_URL not set; skipping live Postgres logout audit tests")


@pytest.fixture(scope="module")
def database_url() -> str:
    return _require_database_url()


@contextmanager
def _connect(database_url: str) -> Iterator[psycopg.Connection]:
    conn = psycopg.connect(database_url, autocommit=False)
    try:
        yield conn
    finally:
        conn.close()


def _reset_public_schema(conn: psycopg.Connection) -> None:
    conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
    conn.execute("CREATE SCHEMA public")
    conn.execute("GRANT ALL ON SCHEMA public TO CURRENT_USER")
    conn.execute("GRANT ALL ON SCHEMA public TO public")
    conn.commit()


@pytest.fixture
def pg_conn(database_url: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(database_url, autocommit=False) as bootstrap:
        _reset_public_schema(bootstrap)
        apply_migrations(bootstrap)
    with psycopg.connect(database_url, row_factory=dict_row, autocommit=False) as conn:
        try:
            yield conn
        finally:
            conn.rollback()
            with psycopg.connect(database_url, autocommit=False) as cleanup:
                _reset_public_schema(cleanup)


def _session_row(
    *,
    token_hash: str,
    session_id: int = 42,
    expires_at: datetime | None = None,
    revoked_at: datetime | None = None,
    csrf_token_hash: str | None = None,
) -> dict[str, Any]:
    return {
        "id": session_id,
        "token_hash": token_hash,
        "admin_username": TEST_USERNAME,
        "created_at": datetime.now(timezone.utc),
        "expires_at": expires_at or (datetime.now(timezone.utc) + timedelta(hours=1)),
        "revoked_at": revoked_at,
        "csrf_token_hash": csrf_token_hash,
    }


def _mock_get_admin_session_by_token_hash(conn: MagicMock, token_hash: str) -> dict[str, Any] | None:
    return _session_store.get(token_hash)


def _mock_revoke_admin_session(conn: MagicMock, *, token_hash: str) -> bool:
    with _session_store_lock:
        row = _session_store.get(token_hash)
        if row is None or row.get("revoked_at") is not None:
            return False
        row["revoked_at"] = datetime.now(timezone.utc)
        return True


@contextmanager
def mock_db_connection() -> Generator[MagicMock, None, None]:
    conn = MagicMock()
    with (
        patch("app.admin_routes.db.db_connection") as db_conn,
        patch(
            "app.admin_routes.db.get_admin_session_by_token_hash",
            _mock_get_admin_session_by_token_hash,
        ),
        patch("app.admin_routes.db.revoke_admin_session", _mock_revoke_admin_session),
    ):
        db_conn.return_value.__enter__.return_value = conn
        db_conn.return_value.__exit__.return_value = None
        yield conn


@contextmanager
def transactional_mock_db_connection() -> Generator[MagicMock, None, None]:
    @contextmanager
    def _transactional_crm_transaction(conn: MagicMock) -> Generator[None, None, None]:
        snapshot = copy.deepcopy(_session_store)
        try:
            with crm_transaction(conn):
                yield
        except Exception:
            with _session_store_lock:
                _session_store.clear()
                _session_store.update(snapshot)
            raise

    with mock_db_connection() as conn:
        with patch("app.admin_routes.crm_transaction", _transactional_crm_transaction):
            yield conn


@contextmanager
def audit_counter() -> Generator[dict[str, int], None, None]:
    counts = {"logout": 0}

    original = audit_service.record_logout

    def _counting_record_logout(*args: Any, **kwargs: Any) -> Any:
        counts["logout"] += 1
        return original(*args, **kwargs)

    with patch("app.admin_routes.audit_service.record_logout", side_effect=_counting_record_logout):
        yield counts


def _count_audit_events(conn: psycopg.Connection) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM audit_events")
        row = cur.fetchone()
    return int(row["n"])


def _count_logout_audit_events(conn: psycopg.Connection) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS n FROM audit_events WHERE action = %s",
            (audit_service.ACTION_AUTH_LOGOUT,),
        )
        row = cur.fetchone()
    return int(row["n"])


def _seed_live_session(
    *,
    raw_token: str | None = None,
    expires_at: datetime | None = None,
    revoked_at: datetime | None = None,
    session_id: int = 42,
) -> tuple[str, str]:
    settings = get_settings()
    raw = raw_token or admin_auth.generate_session_token()
    token_hash = admin_auth.hash_session_token(raw)
    csrf = admin_auth.derive_session_csrf_token(raw, settings)
    _session_store[token_hash] = _session_row(
        token_hash=token_hash,
        session_id=session_id,
        expires_at=expires_at,
        revoked_at=revoked_at,
        csrf_token_hash=admin_auth.hash_csrf_token(csrf),
    )
    return raw, csrf


def _assert_logout_cookie_cleared(response: Any) -> None:
    set_cookie = response.headers.get("set-cookie", "")
    assert SESSION_COOKIE_NAME in set_cookie
    assert "Max-Age=0" in set_cookie or '=""' in set_cookie
    assert "Path=/admin" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=strict" in set_cookie


def _create_pg_session(
    conn: psycopg.Connection,
    *,
    raw_token: str,
    expires_at: datetime | None = None,
    revoked_at: datetime | None = None,
) -> tuple[int, str]:
    settings = get_settings()
    token_hash = admin_auth.hash_session_token(raw_token)
    csrf = admin_auth.derive_session_csrf_token(raw_token, settings)
    session_id = db.create_admin_session(
        conn,
        token_hash=token_hash,
        admin_username=TEST_USERNAME,
        expires_at=expires_at or (datetime.now(timezone.utc) + timedelta(hours=1)),
        csrf_token_hash=admin_auth.hash_csrf_token(csrf),
    )
    if revoked_at is not None:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE admin_sessions SET revoked_at = %s WHERE id = %s",
                (revoked_at, session_id),
            )
    conn.commit()
    return session_id, csrf


@pytest.mark.unit
@pytest.mark.integration
def test_logout_without_cookie_writes_no_audit_event() -> None:
    with mock_db_connection(), audit_counter() as counts:
        before = counts["logout"]
        response = client.post("/admin/logout")
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"
    assert counts["logout"] == before


@pytest.mark.unit
@pytest.mark.integration
def test_logout_with_malformed_cookie_clears_cookie_without_audit() -> None:
    malformed = "not-a-valid-session-token"
    with mock_db_connection(), audit_counter() as counts:
        response = client.post(
            "/admin/logout",
            cookies={SESSION_COOKIE_NAME: malformed},
        )
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"
    _assert_logout_cookie_cleared(response)
    assert counts["logout"] == 0


@pytest.mark.unit
@pytest.mark.integration
def test_logout_with_expired_session_writes_no_audit_event() -> None:
    raw_token, _csrf = _seed_live_session(
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    with mock_db_connection(), audit_counter() as counts:
        response = client.post(
            "/admin/logout",
            cookies={SESSION_COOKIE_NAME: raw_token},
        )
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"
    _assert_logout_cookie_cleared(response)
    assert counts["logout"] == 0


@pytest.mark.unit
@pytest.mark.integration
def test_logout_with_revoked_session_is_idempotent_without_extra_audit() -> None:
    raw_token, _csrf = _seed_live_session(
        revoked_at=datetime.now(timezone.utc),
    )
    with mock_db_connection(), audit_counter() as counts:
        for _ in range(3):
            response = client.post(
                "/admin/logout",
                cookies={SESSION_COOKIE_NAME: raw_token},
            )
            assert response.status_code == 303
            assert response.headers["location"] == "/admin/login"
    assert counts["logout"] == 0


@pytest.mark.unit
@pytest.mark.integration
def test_authenticated_logout_records_one_linked_audit_event() -> None:
    raw_token, csrf = _seed_live_session()
    with mock_db_connection(), audit_counter() as counts:
        response = client.post(
            "/admin/logout",
            data={"csrf_token": csrf},
            cookies={SESSION_COOKIE_NAME: raw_token},
        )
    assert response.status_code == 303
    assert counts["logout"] == 1
    token_hash = admin_auth.hash_session_token(raw_token)
    assert _session_store[token_hash]["revoked_at"] is not None


@pytest.mark.unit
@pytest.mark.integration
def test_logout_with_missing_csrf_keeps_session_active_without_audit() -> None:
    raw_token, _csrf = _seed_live_session()
    token_hash = admin_auth.hash_session_token(raw_token)
    with mock_db_connection(), audit_counter() as counts:
        response = client.post(
            "/admin/logout",
            cookies={SESSION_COOKIE_NAME: raw_token},
        )
    assert response.status_code == 400
    assert admin_auth.INVALID_REQUEST_MESSAGE in response.json()["detail"]
    assert _session_store[token_hash]["revoked_at"] is None
    assert counts["logout"] == 0


@pytest.mark.unit
@pytest.mark.integration
def test_logout_with_invalid_csrf_keeps_session_active_without_audit() -> None:
    raw_token, _csrf = _seed_live_session()
    token_hash = admin_auth.hash_session_token(raw_token)
    with mock_db_connection(), audit_counter() as counts:
        response = client.post(
            "/admin/logout",
            data={"csrf_token": "wrong-token"},
            cookies={SESSION_COOKIE_NAME: raw_token},
        )
    assert response.status_code == 400
    assert admin_auth.INVALID_REQUEST_MESSAGE in response.json()["detail"]
    assert _session_store[token_hash]["revoked_at"] is None
    assert counts["logout"] == 0


@pytest.mark.unit
@pytest.mark.integration
def test_logout_rejects_cross_session_csrf_without_audit() -> None:
    settings = get_settings()
    raw_a = admin_auth.generate_session_token()
    raw_b = admin_auth.generate_session_token()
    csrf_a = admin_auth.derive_session_csrf_token(raw_a, settings)
    _session_store[admin_auth.hash_session_token(raw_b)] = _session_row(
        token_hash=admin_auth.hash_session_token(raw_b),
        session_id=2,
    )
    with mock_db_connection(), audit_counter() as counts:
        response = client.post(
            "/admin/logout",
            data={"csrf_token": csrf_a},
            cookies={SESSION_COOKIE_NAME: raw_b},
        )
    assert response.status_code == 400
    assert admin_auth.INVALID_REQUEST_MESSAGE in response.json()["detail"]
    assert _session_store[admin_auth.hash_session_token(raw_b)]["revoked_at"] is None
    assert counts["logout"] == 0


@pytest.mark.unit
@pytest.mark.integration
def test_logout_audit_failure_rolls_back_session_revocation() -> None:
    raw_token, csrf = _seed_live_session()
    token_hash = admin_auth.hash_session_token(raw_token)
    with transactional_mock_db_connection(), audit_counter():
        with patch(
            "app.admin_routes.audit_service.record_logout",
            side_effect=RuntimeError("audit insert failed"),
        ):
            with pytest.raises(RuntimeError, match="audit insert failed"):
                client.post(
                    "/admin/logout",
                    data={"csrf_token": csrf},
                    cookies={SESSION_COOKIE_NAME: raw_token},
                )
    assert _session_store[token_hash]["revoked_at"] is None


@pytest.mark.unit
@pytest.mark.integration
def test_cross_site_shaped_anonymous_logout_writes_no_audit_event() -> None:
    with mock_db_connection(), audit_counter() as counts:
        response = client.post(
            "/admin/logout",
            headers={
                "Origin": "https://evil.example",
                "Referer": "https://evil.example/attack",
            },
        )
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"
    assert counts["logout"] == 0


@pytest.mark.unit
@pytest.mark.integration
def test_concurrent_authenticated_logout_writes_single_audit_event() -> None:
    raw_token, csrf = _seed_live_session()
    with mock_db_connection(), audit_counter() as counts:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(
                    client.post,
                    "/admin/logout",
                    data={"csrf_token": csrf},
                    cookies={SESSION_COOKIE_NAME: raw_token},
                )
                for _ in range(2)
            ]
            responses = [future.result() for future in futures]
    assert all(response.status_code == 303 for response in responses)
    assert counts["logout"] == 1


@pytest.mark.integration
def test_postgres_logout_without_cookie_writes_no_audit_event(
    pg_conn: psycopg.Connection,
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", database_url)
    before = _count_audit_events(pg_conn)
    response = client.post("/admin/logout")
    assert response.status_code == 303
    assert _count_audit_events(pg_conn) == before


@pytest.mark.integration
def test_postgres_authenticated_logout_transaction_failure_rolls_back(
    pg_conn: psycopg.Connection,
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", database_url)
    raw_token = admin_auth.generate_session_token()
    session_id, csrf = _create_pg_session(pg_conn, raw_token=raw_token)
    token_hash = admin_auth.hash_session_token(raw_token)

    with patch(
        "app.admin_routes.audit_service.record_logout",
        side_effect=RuntimeError("audit insert failed"),
    ):
        with pytest.raises(RuntimeError, match="audit insert failed"):
            client.post(
                "/admin/logout",
                data={"csrf_token": csrf},
                cookies={SESSION_COOKIE_NAME: raw_token},
            )

    pg_conn.row_factory = dict_row
    row = pg_conn.execute(
        "SELECT revoked_at FROM admin_sessions WHERE id = %s",
        (session_id,),
    ).fetchone()
    assert row is not None
    assert row["revoked_at"] is None
    assert _count_logout_audit_events(pg_conn) == 0


@pytest.mark.integration
def test_postgres_concurrent_authenticated_logout_writes_single_audit_event(
    pg_conn: psycopg.Connection,
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", database_url)
    raw_token = admin_auth.generate_session_token()
    session_id, csrf = _create_pg_session(pg_conn, raw_token=raw_token)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                client.post,
                "/admin/logout",
                data={"csrf_token": csrf},
                cookies={SESSION_COOKIE_NAME: raw_token},
            )
            for _ in range(2)
        ]
        responses = [future.result() for future in futures]

    assert all(response.status_code == 303 for response in responses)
    assert _count_logout_audit_events(pg_conn) == 1

    pg_conn.row_factory = dict_row
    row = pg_conn.execute(
        "SELECT revoked_at FROM admin_sessions WHERE id = %s",
        (session_id,),
    ).fetchone()
    assert row is not None
    assert row["revoked_at"] is not None
