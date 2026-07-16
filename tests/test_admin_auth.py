"""Tests for admin authentication, sessions, CSRF, and route protection."""

from __future__ import annotations

import copy
import re
import threading
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Generator
from unittest.mock import MagicMock, patch

import pytest
from argon2 import PasswordHasher
from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.testclient import TestClient
from starlette.types import ASGIApp, Receive, Scope, Send

from app import admin_auth
from app import db
from app.admin_auth import LOGIN_FLOW_COOKIE_NAME, SESSION_COOKIE_NAME
from app.admin_routes import _issue_session
from app.config import get_settings
from app.crm_uow import crm_transaction
from app.main import app

TEST_TRUSTED_PROXY_CIDRS = "10.0.0.0/8"
TEST_TRUSTED_EDGE_CIDRS = "172.68.0.0/16"
TEST_RENDER_PEER = "10.0.0.1"
TEST_CF_EDGE = "172.68.1.1"


class _PeerOverrideMiddleware:
    """Test-only ASGI wrapper to simulate trusted proxy peers."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = dict(scope.get("headers") or [])
            override = headers.get(b"x-test-client-host")
            if override:
                mutable_scope = dict(scope)
                mutable_scope["client"] = (override.decode("ascii"), 0)
                scope = mutable_scope
        await self.app(scope, receive, send)


client = TestClient(_PeerOverrideMiddleware(app), follow_redirects=False)

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "test-session-secret-32chars-minimum"
TEST_LIMITER_SECRET = "test-login-limiter-secret-32chars-min"

_login_flows: dict[str, dict[str, Any]] = {}
_session_store: dict[str, dict[str, Any]] = {}
_login_flow_lock = threading.Lock()


class FakeRateLimitStore:
    """In-memory Postgres stand-in for shared login rate-limit state."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def is_throttled(self, limiter_key: str, now: datetime) -> bool:
        with self._lock:
            row = self.rows.get(limiter_key)
            if row is None:
                return False
            locked_until = row.get("locked_until")
            if locked_until is None:
                return False
            return locked_until > now

    def try_admit(
        self,
        limiter_keys: tuple[str, ...],
        now: datetime,
        *,
        rate_limit: int,
        window_seconds: int,
        lockout_seconds: int,
    ) -> db.AdminLoginAdmission:
        with self._lock:
            ordered_keys = tuple(sorted(limiter_keys))
            for limiter_key in ordered_keys:
                if limiter_key not in self.rows:
                    self.rows[limiter_key] = {
                        "failure_count": 0,
                        "window_started_at": now,
                        "locked_until": None,
                        "updated_at": now,
                    }

            for limiter_key in ordered_keys:
                row = self.rows[limiter_key]
                locked_until = row.get("locked_until")
                if locked_until is not None and locked_until > now:
                    return db.AdminLoginAdmission(
                        admitted=False,
                        throttled=True,
                        already_locked=True,
                        lockout_transition=False,
                    )

            lockout_transition = False
            for limiter_key in ordered_keys:
                row = self.rows[limiter_key]
                window_start = now - timedelta(seconds=window_seconds)
                if row["window_started_at"] < window_start:
                    failure_count = 1
                    window_started_at = now
                else:
                    failure_count = row["failure_count"] + 1
                    window_started_at = row["window_started_at"]

                prior_locked_until = row.get("locked_until")
                locked_until = prior_locked_until
                if failure_count >= rate_limit:
                    locked_until = now + timedelta(seconds=lockout_seconds)
                    if prior_locked_until is None or prior_locked_until <= now:
                        lockout_transition = True

                self.rows[limiter_key] = {
                    "failure_count": failure_count,
                    "window_started_at": window_started_at,
                    "locked_until": locked_until,
                    "updated_at": now,
                }

            return db.AdminLoginAdmission(
                admitted=True,
                throttled=False,
                already_locked=False,
                lockout_transition=lockout_transition,
            )

    def clear_many(self, limiter_keys: tuple[str, ...]) -> None:
        with self._lock:
            for limiter_key in limiter_keys:
                self.rows.pop(limiter_key, None)

    def release_admission(self, limiter_key: str, *, rate_limit: int) -> None:
        with self._lock:
            row = self.rows.get(limiter_key)
            if row is None:
                return
            failure_count = max(int(row["failure_count"]) - 1, 0)
            locked_until = row.get("locked_until")
            if failure_count < rate_limit:
                locked_until = None
            self.rows[limiter_key] = {
                **row,
                "failure_count": failure_count,
                "locked_until": locked_until,
            }

    def cleanup(
        self,
        now: datetime,
        *,
        window_seconds: int,
        lockout_seconds: int,
        batch_size: int,
    ) -> int:
        with self._lock:
            retention = max(window_seconds, lockout_seconds) * 2
            cutoff = now - timedelta(seconds=retention)
            expired = sorted(
                key
                for key, row in self.rows.items()
                if row["updated_at"] < cutoff
                and (row["locked_until"] is None or row["locked_until"] < now)
            )[:batch_size]
            for key in expired:
                del self.rows[key]
            return len(expired)


@pytest.fixture
def rate_limit_store() -> FakeRateLimitStore:
    return FakeRateLimitStore()


@contextmanager
def shared_rate_limiter(store: FakeRateLimitStore) -> Generator[None, None, None]:
    """Patch db rate-limit functions to use shared durable storage."""

    def is_throttled(conn: Any, *, limiter_key: str, now: datetime) -> bool:
        return store.is_throttled(limiter_key, now)

    def try_admit(
        conn: Any,
        *,
        limiter_keys: tuple[str, ...],
        now: datetime,
        rate_limit: int,
        window_seconds: int,
        lockout_seconds: int,
    ) -> db.AdminLoginAdmission:
        return store.try_admit(
            limiter_keys,
            now,
            rate_limit=rate_limit,
            window_seconds=window_seconds,
            lockout_seconds=lockout_seconds,
        )

    def clear_many(conn: Any, *, limiter_keys: tuple[str, ...]) -> None:
        store.clear_many(limiter_keys)

    def release_admission(
        conn: Any,
        *,
        limiter_key: str,
        now: datetime,
        rate_limit: int,
    ) -> None:
        store.release_admission(limiter_key, rate_limit=rate_limit)

    def cleanup(
        conn: Any,
        *,
        now: datetime,
        window_seconds: int,
        lockout_seconds: int,
        batch_size: int,
    ) -> int:
        return store.cleanup(
            now,
            window_seconds=window_seconds,
            lockout_seconds=lockout_seconds,
            batch_size=batch_size,
        )

    with (
        patch("app.admin_auth.db.is_admin_login_throttled", side_effect=is_throttled),
        patch("app.admin_auth.db.try_admit_admin_login", side_effect=try_admit),
        patch("app.admin_auth.db.clear_admin_login_rate_limits", side_effect=clear_many),
        patch("app.admin_auth.db.release_admin_login_admission", side_effect=release_admission),
        patch(
            "app.admin_auth.db.cleanup_expired_admin_login_rate_limits",
            side_effect=cleanup,
        ),
        patch("app.admin_auth.db.db_connection") as db_conn,
    ):
        db_conn.return_value.__enter__.return_value = MagicMock()
        db_conn.return_value.__exit__.return_value = None
        yield


@pytest.fixture(autouse=True)
def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "5")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_WINDOW_SECONDS", "900")
    monkeypatch.setenv("ADMIN_LOGIN_LOCKOUT_SECONDS", "900")
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    monkeypatch.delenv("ADMIN_TRUSTED_EDGE_CIDRS", raising=False)
    admin_auth.reset_login_rate_limiter()
    _login_flows.clear()
    _session_store.clear()


def _mock_create_admin_login_flow(conn: MagicMock, **kwargs: Any) -> int:
    flow_hash = kwargs["flow_token_hash"]
    _login_flows[flow_hash] = {
        "id": len(_login_flows) + 1,
        "flow_token_hash": flow_hash,
        "csrf_token_hash": kwargs["csrf_token_hash"],
        "created_at": datetime.now(timezone.utc),
        "expires_at": kwargs["expires_at"],
        "consumed_at": None,
    }
    return int(_login_flows[flow_hash]["id"])


def _mock_cleanup_stale_admin_login_flows(conn: MagicMock, **kwargs: Any) -> int:
    now = kwargs["now"]
    expired_cutoff = now - timedelta(seconds=kwargs["expired_retention_seconds"])
    consumed_cutoff = now - timedelta(seconds=kwargs["consumed_retention_seconds"])
    batch_size = kwargs["batch_size"]
    stale_hashes = sorted(
        flow_hash
        for flow_hash, row in _login_flows.items()
        if (
            row["consumed_at"] is None
            and row["expires_at"] < expired_cutoff
        )
        or (
            row["consumed_at"] is not None
            and row["consumed_at"] < consumed_cutoff
        )
    )[:batch_size]
    for flow_hash in stale_hashes:
        del _login_flows[flow_hash]
    return len(stale_hashes)


def _mock_claim_admin_login_flow(
    conn: MagicMock,
    *,
    flow_token_hash: str,
    csrf_token_hash: str,
    now: datetime,
) -> dict[str, Any] | None:
    with _login_flow_lock:
        row = _login_flows.get(flow_token_hash)
        if row is None:
            return None
        if row.get("consumed_at") is not None:
            return None
        expires_at = row["expires_at"]
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= now:
            return None
        if row.get("csrf_token_hash") != csrf_token_hash:
            return None
        row["consumed_at"] = now
        return dict(row)


def _mock_consume_admin_login_flow(
    conn: MagicMock,
    *,
    flow_token_hash: str,
    now: datetime,
) -> bool:
    with _login_flow_lock:
        row = _login_flows.get(flow_token_hash)
        if row is None:
            return False
        if row.get("consumed_at") is not None:
            return False
        expires_at = row["expires_at"]
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= now:
            return False
        row["consumed_at"] = now
        return True


def _mock_create_admin_session(conn: MagicMock, **kwargs: Any) -> int:
    session_id = len(_session_store) + 1
    _session_store[kwargs["token_hash"]] = {
        "id": session_id,
        "token_hash": kwargs["token_hash"],
        "admin_username": kwargs["admin_username"],
        "created_at": datetime.now(timezone.utc),
        "expires_at": kwargs["expires_at"],
        "revoked_at": None,
        "csrf_token_hash": kwargs.get("csrf_token_hash"),
    }
    return session_id


def _mock_get_admin_session_by_token_hash(
    conn: MagicMock,
    token_hash: str,
) -> dict[str, Any] | None:
    return _session_store.get(token_hash)


def _mock_update_admin_session_csrf(
    conn: MagicMock,
    *,
    session_id: int,
    csrf_token_hash: str,
) -> None:
    for row in _session_store.values():
        if row["id"] == session_id:
            row["csrf_token_hash"] = csrf_token_hash


def _mock_revoke_admin_session(conn: MagicMock, *, token_hash: str) -> bool:
    row = _session_store.get(token_hash)
    if row is None or row.get("revoked_at") is not None:
        return False
    row["revoked_at"] = datetime.now(timezone.utc)
    return True


@contextmanager
def mock_db_connection() -> Generator[MagicMock, None, None]:
    conn = MagicMock()
    with ExitStack() as stack:
        db_conn_patch = stack.enter_context(patch("app.admin_routes.db.db_connection"))
        stack.enter_context(
            patch("app.admin_routes.db.create_admin_login_flow", _mock_create_admin_login_flow)
        )
        stack.enter_context(
            patch(
                "app.admin_routes.db.cleanup_stale_admin_login_flows",
                _mock_cleanup_stale_admin_login_flows,
            )
        )
        stack.enter_context(
            patch("app.admin_routes.db.claim_admin_login_flow", _mock_claim_admin_login_flow)
        )
        stack.enter_context(
            patch("app.admin_routes.db.consume_admin_login_flow", _mock_consume_admin_login_flow)
        )
        stack.enter_context(
            patch("app.admin_routes.db.create_admin_session", _mock_create_admin_session)
        )
        stack.enter_context(
            patch(
                "app.admin_routes.db.get_admin_session_by_token_hash",
                _mock_get_admin_session_by_token_hash,
            )
        )
        stack.enter_context(
            patch("app.admin_routes.db.update_admin_session_csrf", _mock_update_admin_session_csrf)
        )
        stack.enter_context(
            patch("app.admin_routes.db.revoke_admin_session", _mock_revoke_admin_session)
        )
        db_conn_patch.return_value.__enter__.return_value = conn
        db_conn_patch.return_value.__exit__.return_value = None
        yield conn


@contextmanager
def transactional_mock_db_connection() -> Generator[MagicMock, None, None]:
    """Mock DB with in-memory session store that rolls back on transaction failure."""

    @contextmanager
    def _transactional_crm_transaction(conn: MagicMock) -> Generator[None, None, None]:
        snapshot = copy.deepcopy(_session_store)
        try:
            with crm_transaction(conn):
                yield
        except Exception:
            _session_store.clear()
            _session_store.update(snapshot)
            raise

    with mock_db_connection() as conn:
        with patch("app.admin_routes.crm_transaction", _transactional_crm_transaction):
            yield conn


def _extract_csrf_token(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match is not None
    return match.group(1)


def _parse_login_form(response: Any) -> tuple[str, dict[str, str]]:
    csrf_token = _extract_csrf_token(response.text)
    cookies: dict[str, str] = {}
    flow_cookie = response.cookies.get(LOGIN_FLOW_COOKIE_NAME)
    if flow_cookie:
        cookies[LOGIN_FLOW_COOKIE_NAME] = flow_cookie
    return csrf_token, cookies


def _login_flow_set_cookie_headers(response: Any) -> list[str]:
    """Return raw Set-Cookie header values for the pre-auth login-flow cookie."""
    if hasattr(response.headers, "get_list"):
        raw_headers = response.headers.get_list("set-cookie")
    else:
        combined = response.headers.get("set-cookie", "")
        raw_headers = [combined] if combined else []
    return [header for header in raw_headers if LOGIN_FLOW_COOKIE_NAME in header]


def _assert_replacement_login_flow_cookie_retained(response: Any) -> None:
    """Failed auth must retain exactly one non-expiring replacement flow cookie."""
    headers = _login_flow_set_cookie_headers(response)
    assert len(headers) == 1, headers
    assert "Max-Age=0" not in headers[0]
    assert response.cookies.get(LOGIN_FLOW_COOKIE_NAME)


def _fetch_login_form() -> tuple[str, dict[str, str]]:
    with mock_db_connection():
        response = client.get("/admin/login")
    return _parse_login_form(response)


def _login(
    *,
    username: str = TEST_USERNAME,
    password: str = TEST_PASSWORD,
    csrf_token: str | None = None,
    cookies: dict[str, str] | None = None,
    next_path: str | None = None,
    headers: dict[str, str] | None = None,
) -> Any:
    with mock_db_connection():
        if csrf_token is None:
            if cookies is None:
                csrf_token, cookies = _parse_login_form(client.get("/admin/login"))
            else:
                raise ValueError("csrf_token is required when cookies are provided")
        data = {
            "username": username,
            "password": password,
            "csrf_token": csrf_token,
        }
        if next_path is not None:
            data["next"] = next_path
        return client.post("/admin/login", data=data, cookies=cookies or {}, headers=headers or {})


def _extract_session_cookie(response: Any) -> str | None:
    cookie = response.cookies.get(SESSION_COOKIE_NAME)
    return cookie


def _session_row(
    *,
    token_hash: str,
    expires_at: datetime | None = None,
    revoked_at: datetime | None = None,
    csrf_token_hash: str | None = None,
    session_id: int = 1,
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


def _request_with_client(host: str) -> Request:
    scope = {
        "type": "http",
        "headers": [],
        "client": (host, 12345),
        "method": "POST",
        "path": "/admin/login",
    }
    return Request(scope)


@pytest.mark.unit
def test_admin_preview_mode_allows_dashboard_without_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("BASE_URL", "http://127.0.0.1:8765")
    monkeypatch.delenv("ADMIN_USERNAME", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD_HASH", raising=False)
    monkeypatch.delenv("ADMIN_SESSION_SECRET", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    dash = client.get("/admin")
    assert dash.status_code == 200
    assert "Preview data — not production" in dash.text
    assert "Overdue next actions" in dash.text
    assert "Companies by funding stage" in dash.text
    login = client.get("/admin/login")
    assert login.status_code == 200
    assert "Admin sign in" in login.text


@pytest.mark.unit
def test_admin_preview_mode_disabled_on_production_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("BASE_URL", "https://saberistic.com")
    settings = get_settings()
    assert settings.admin_preview_mode is True
    assert settings.admin_preview_enabled is False


@pytest.mark.unit
def test_admin_auth_settings_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ADMIN_USERNAME", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD_HASH", raising=False)
    monkeypatch.delenv("ADMIN_SESSION_SECRET", raising=False)
    monkeypatch.delenv("ADMIN_LOGIN_LIMITER_SECRET", raising=False)
    settings = get_settings()
    assert not settings.admin_auth_configured


@pytest.mark.unit
def test_verify_admin_credentials_accepts_valid_pair() -> None:
    settings = get_settings()
    assert admin_auth.verify_admin_credentials(TEST_USERNAME, TEST_PASSWORD, settings)


@pytest.mark.unit
def test_verify_admin_credentials_rejects_wrong_password() -> None:
    settings = get_settings()
    assert not admin_auth.verify_admin_credentials(TEST_USERNAME, "wrong-password", settings)


@pytest.mark.unit
def test_verify_admin_credentials_rejects_wrong_username() -> None:
    settings = get_settings()
    assert not admin_auth.verify_admin_credentials("unknown-user", TEST_PASSWORD, settings)


@pytest.mark.unit
def test_csrf_value_round_trip() -> None:
    raw = admin_auth.generate_csrf_value()
    stored = admin_auth.hash_csrf_token(raw)
    assert admin_auth.verify_csrf_value(raw, stored)


@pytest.mark.unit
def test_csrf_value_rejects_tampered_token() -> None:
    raw = admin_auth.generate_csrf_value()
    stored = admin_auth.hash_csrf_token(raw)
    tampered = raw[:-1] + ("a" if raw[-1] != "a" else "b")
    assert not admin_auth.verify_csrf_value(tampered, stored)


@pytest.mark.unit
def test_csrf_value_rejects_missing_or_malformed() -> None:
    stored = admin_auth.hash_csrf_token(admin_auth.generate_csrf_value())
    assert not admin_auth.verify_csrf_value("", stored)
    assert not admin_auth.verify_csrf_value("bad-token", None)
    assert not admin_auth.verify_csrf_value("bad-token", "")


@pytest.mark.unit
def test_build_source_and_account_rate_limit_keys() -> None:
    settings = get_settings()
    source_a = admin_auth.build_source_rate_limit_key("203.0.113.1", settings)
    source_b = admin_auth.build_source_rate_limit_key("203.0.113.2", settings)
    assert source_a != source_b
    assert len(source_a) == 64

    account_a = admin_auth.build_account_rate_limit_key("Operator", settings)
    account_b = admin_auth.build_account_rate_limit_key("operator", settings)
    assert account_a == account_b


@pytest.mark.unit
def test_login_limiter_keys_include_candidate_for_configured_username() -> None:
    settings = get_settings()
    keys = admin_auth.login_limiter_keys(
        submitted_username="Operator",
        client_source="203.0.113.1",
        configured_admin_username="operator",
        settings=settings,
    )
    assert len(keys) == 2
    assert admin_auth.build_source_rate_limit_key("203.0.113.1", settings) in keys
    assert admin_auth.build_candidate_rate_limit_key("operator", settings) in keys


@pytest.mark.unit
def test_login_limiter_keys_include_candidate_for_unknown_username() -> None:
    settings = get_settings()
    keys = admin_auth.login_limiter_keys(
        submitted_username="ghost",
        client_source="203.0.113.1",
        configured_admin_username="operator",
        settings=settings,
    )
    assert len(keys) == 2
    assert admin_auth.build_source_rate_limit_key("203.0.113.1", settings) in keys
    assert admin_auth.build_candidate_rate_limit_key("ghost", settings) in keys


@pytest.mark.unit
def test_build_rate_limit_key_hashes_username_and_source() -> None:
    key_a = admin_auth.build_rate_limit_key("Operator", "203.0.113.1")
    key_b = admin_auth.build_rate_limit_key("operator", "203.0.113.1")
    key_c = admin_auth.build_rate_limit_key("operator", "203.0.113.2")
    assert key_a == key_b
    assert key_a != key_c
    assert len(key_a) == 64


@pytest.mark.unit
def test_client_ip_ignores_forwarded_without_trusted_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    settings = get_settings()
    request = _request_with_client("198.51.100.10")
    request.headers.__dict__["_list"].append((b"x-forwarded-for", b"203.0.113.99"))
    assert admin_auth.client_ip(request, settings) == "198.51.100.10"


@pytest.mark.unit
def test_client_ip_uses_trusted_xff_chain_when_peer_is_trusted_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", TEST_TRUSTED_PROXY_CIDRS)
    monkeypatch.setenv("ADMIN_TRUSTED_EDGE_CIDRS", TEST_TRUSTED_EDGE_CIDRS)
    settings = get_settings()
    request = _request_with_client(TEST_RENDER_PEER)
    request.headers.__dict__["_list"].append(
        (b"x-forwarded-for", f"203.0.113.50, {TEST_CF_EDGE}".encode())
    )
    assert admin_auth.client_ip(request, settings) == "203.0.113.50"


@pytest.mark.unit
def test_client_ip_ignores_spoofed_leftmost_without_edge_hop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", TEST_TRUSTED_PROXY_CIDRS)
    monkeypatch.setenv("ADMIN_TRUSTED_EDGE_CIDRS", TEST_TRUSTED_EDGE_CIDRS)
    settings = get_settings()
    request = _request_with_client(TEST_RENDER_PEER)
    request.headers.__dict__["_list"].append(
        (b"x-forwarded-for", b"203.0.113.50, 198.51.100.10")
    )
    assert admin_auth.client_ip(request, settings) == TEST_RENDER_PEER


@pytest.mark.unit
@pytest.mark.integration
def test_login_logout_flow(rate_limit_store: FakeRateLimitStore) -> None:
    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            login = _login()
            assert login.status_code == 303
            assert login.headers["location"] == "/admin"
            session_cookie = _extract_session_cookie(login)
            assert session_cookie

            token_hash = admin_auth.hash_session_token(session_cookie)
            assert token_hash in _session_store

            dashboard = client.get("/admin", cookies={SESSION_COOKIE_NAME: session_cookie})
            assert dashboard.status_code == 200
            assert TEST_USERNAME in dashboard.text
            logout_csrf = _extract_csrf_token(dashboard.text)

            logout = client.post(
                "/admin/logout",
                data={"csrf_token": logout_csrf},
                cookies={SESSION_COOKIE_NAME: session_cookie},
            )
            assert logout.status_code == 303
            assert logout.headers["location"] == "/admin/login"
            assert _session_store[token_hash]["revoked_at"] is not None


@pytest.mark.unit
@pytest.mark.integration
def test_anonymous_admin_dashboard_redirects_to_login() -> None:
    response = client.get("/admin")
    assert response.status_code == 303
    assert response.headers["location"].startswith("/admin/login?next=")


@pytest.mark.unit
@pytest.mark.integration
def test_anonymous_nested_admin_route_redirects_to_login() -> None:
    response = client.get("/admin/reports")
    assert response.status_code == 303
    assert "/admin/login" in response.headers["location"]


@pytest.mark.unit
@pytest.mark.integration
def test_login_invalid_credentials_use_generic_message(rate_limit_store: FakeRateLimitStore) -> None:
    with shared_rate_limiter(rate_limit_store):
        response = _login(password="not-the-password")
    assert response.status_code == 401
    assert admin_auth.INVALID_CREDENTIALS_MESSAGE in response.text
    _assert_replacement_login_flow_cookie_retained(response)


@pytest.mark.unit
@pytest.mark.integration
def test_login_retry_after_wrong_password_without_refresh(
    rate_limit_store: FakeRateLimitStore,
) -> None:
    """Wrong password then corrected password on the same client without refresh."""
    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            form = client.get("/admin/login")
            csrf_token, _ = _parse_login_form(form)

            failed = client.post(
                "/admin/login",
                data={
                    "username": TEST_USERNAME,
                    "password": "wrong-password",
                    "csrf_token": csrf_token,
                },
            )
            assert failed.status_code == 401
            assert admin_auth.INVALID_CREDENTIALS_MESSAGE in failed.text
            _assert_replacement_login_flow_cookie_retained(failed)

            retry_csrf = _extract_csrf_token(failed.text)
            assert retry_csrf != csrf_token

            success = client.post(
                "/admin/login",
                data={
                    "username": TEST_USERNAME,
                    "password": TEST_PASSWORD,
                    "csrf_token": retry_csrf,
                },
            )
            assert success.status_code == 303
            assert success.headers["location"] == "/admin"
            assert _extract_session_cookie(success)


@pytest.mark.unit
@pytest.mark.integration
def test_login_invalid_csrf_retains_replacement_flow_cookie(
    rate_limit_store: FakeRateLimitStore,
) -> None:
    csrf_token, cookies = _fetch_login_form()
    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            response = client.post(
                "/admin/login",
                data={
                    "username": TEST_USERNAME,
                    "password": TEST_PASSWORD,
                    "csrf_token": "not-a-valid-token",
                },
                cookies=cookies,
            )
    assert response.status_code == 400
    assert admin_auth.INVALID_CREDENTIALS_MESSAGE in response.text
    _assert_replacement_login_flow_cookie_retained(response)


@pytest.mark.unit
@pytest.mark.integration
def test_login_rate_limit_keeps_existing_flow_when_throttled(
    rate_limit_store: FakeRateLimitStore,
) -> None:
    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            csrf_token, cookies = _fetch_login_form()
            for _ in range(5):
                response = _login(password="wrong", csrf_token=csrf_token, cookies=cookies)
                assert response.status_code == 401
                csrf_token = _extract_csrf_token(response.text)
                flow_cookie = response.cookies.get(LOGIN_FLOW_COOKIE_NAME)
                if flow_cookie:
                    cookies[LOGIN_FLOW_COOKIE_NAME] = flow_cookie

            blocked = client.post(
                "/admin/login",
                data={
                    "username": TEST_USERNAME,
                    "password": "wrong",
                    "csrf_token": csrf_token,
                },
                cookies=cookies,
            )
    assert blocked.status_code == 429
    assert admin_auth.LOGIN_THROTTLED_MESSAGE in blocked.text
    assert _login_flow_set_cookie_headers(blocked) == []


@pytest.mark.unit
@pytest.mark.integration
def test_login_unknown_username_uses_same_error_message(rate_limit_store: FakeRateLimitStore) -> None:
    with shared_rate_limiter(rate_limit_store):
        bad_password = _login(username="ghost", password="nope")
        bad_username = _login(username="ghost", password=TEST_PASSWORD)
    assert bad_password.status_code == 401
    assert bad_username.status_code == 401
    assert admin_auth.INVALID_CREDENTIALS_MESSAGE in bad_password.text
    assert admin_auth.INVALID_CREDENTIALS_MESSAGE in bad_username.text


@pytest.mark.unit
@pytest.mark.integration
def test_login_rejects_invalid_csrf_token(rate_limit_store: FakeRateLimitStore) -> None:
    csrf_token, cookies = _fetch_login_form()
    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            response = client.post(
                "/admin/login",
                data={
                    "username": TEST_USERNAME,
                    "password": TEST_PASSWORD,
                    "csrf_token": "not-a-valid-token",
                },
                cookies=cookies,
            )
    assert response.status_code == 400
    assert admin_auth.INVALID_CREDENTIALS_MESSAGE in response.text


@pytest.mark.unit
@pytest.mark.integration
def test_login_rejects_cross_session_csrf_token(rate_limit_store: FakeRateLimitStore) -> None:
    csrf_a, _cookies_a = _fetch_login_form()
    _csrf_b, cookies_b = _fetch_login_form()
    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            response = client.post(
                "/admin/login",
                data={
                    "username": TEST_USERNAME,
                    "password": TEST_PASSWORD,
                    "csrf_token": csrf_a,
                },
                cookies=cookies_b,
            )
    assert response.status_code == 400
    assert admin_auth.INVALID_CREDENTIALS_MESSAGE in response.text


@pytest.mark.unit
@pytest.mark.integration
def test_login_rejects_missing_login_flow_cookie(rate_limit_store: FakeRateLimitStore) -> None:
    csrf_token, _cookies = _fetch_login_form()
    client.cookies.clear()
    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            response = client.post(
                "/admin/login",
                data={
                    "username": TEST_USERNAME,
                    "password": TEST_PASSWORD,
                    "csrf_token": csrf_token,
                },
            )
    assert response.status_code == 400
    assert admin_auth.INVALID_CREDENTIALS_MESSAGE in response.text


@pytest.mark.unit
@pytest.mark.integration
def test_login_rejects_expired_login_flow(rate_limit_store: FakeRateLimitStore) -> None:
    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            csrf_token, cookies = _fetch_login_form()
            flow_cookie = cookies[LOGIN_FLOW_COOKIE_NAME]
            flow_hash = admin_auth.hash_session_token(flow_cookie)
            _login_flows[flow_hash]["expires_at"] = datetime.now(timezone.utc) - timedelta(
                seconds=1
            )
            response = client.post(
                "/admin/login",
                data={
                    "username": TEST_USERNAME,
                    "password": TEST_PASSWORD,
                    "csrf_token": csrf_token,
                },
                cookies=cookies,
            )
    assert response.status_code == 400
    assert admin_auth.INVALID_CREDENTIALS_MESSAGE in response.text


@pytest.mark.unit
@pytest.mark.integration
def test_login_rejects_replayed_login_flow(rate_limit_store: FakeRateLimitStore) -> None:
    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            csrf_token, cookies = _fetch_login_form()
            first = client.post(
                "/admin/login",
                data={
                    "username": TEST_USERNAME,
                    "password": "wrong-password",
                    "csrf_token": csrf_token,
                },
                cookies=cookies,
            )
            assert first.status_code == 401

            replay = client.post(
                "/admin/login",
                data={
                    "username": TEST_USERNAME,
                    "password": TEST_PASSWORD,
                    "csrf_token": csrf_token,
                },
                cookies=cookies,
            )
    assert replay.status_code == 400
    assert admin_auth.INVALID_CREDENTIALS_MESSAGE in replay.text


@pytest.mark.unit
@pytest.mark.integration
def test_login_rate_limiting(rate_limit_store: FakeRateLimitStore) -> None:
    with shared_rate_limiter(rate_limit_store):
        for _ in range(5):
            response = _login(password="wrong")
            assert response.status_code == 401

        blocked = _login(password="wrong")
        assert blocked.status_code == 429
        assert admin_auth.LOGIN_THROTTLED_MESSAGE in blocked.text


@pytest.mark.unit
@pytest.mark.integration
def test_login_rate_limiting_enforced_across_instances(rate_limit_store: FakeRateLimitStore) -> None:
    with shared_rate_limiter(rate_limit_store):
        for _ in range(5):
            assert _login(password="wrong").status_code == 401

        blocked = _login(password="wrong")
        assert blocked.status_code == 429
        assert admin_auth.LOGIN_THROTTLED_MESSAGE in blocked.text


@pytest.mark.unit
@pytest.mark.integration
def test_successful_login_clears_candidate_rate_limit_only(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    settings = get_settings()
    source_key = admin_auth.build_source_rate_limit_key("testclient", settings)
    candidate_key = admin_auth.build_candidate_rate_limit_key(TEST_USERNAME, settings)
    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            assert _login(password="wrong").status_code == 401

            recovery = _login()
            assert recovery.status_code == 303
            client.cookies.pop(SESSION_COOKIE_NAME, None)
            assert candidate_key not in rate_limit_store.rows
            assert source_key in rate_limit_store.rows

            assert _login(password="wrong").status_code == 401
            assert _login(password="wrong").status_code == 429


@pytest.mark.unit
@pytest.mark.integration
def test_rate_limit_expires_after_lockout(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    monkeypatch.setenv("ADMIN_LOGIN_LOCKOUT_SECONDS", "1")
    with shared_rate_limiter(rate_limit_store):
        assert _login(password="wrong").status_code == 401
        assert _login(password="wrong").status_code == 401
        assert _login(password="wrong").status_code == 429

        settings = get_settings()
        source_key = admin_auth.build_source_rate_limit_key("testclient", settings)
        candidate_key = admin_auth.build_candidate_rate_limit_key(TEST_USERNAME, settings)
        expired_lock = datetime.now(timezone.utc) - timedelta(seconds=1)
        for key in (source_key, candidate_key):
            rate_limit_store.rows[key]["locked_until"] = expired_lock

        allowed = _login(password="wrong")
        assert allowed.status_code == 401


@pytest.mark.unit
@pytest.mark.integration
def test_rate_limit_uses_trusted_xff_chain_when_peer_is_trusted_proxy(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", TEST_TRUSTED_PROXY_CIDRS)
    monkeypatch.setenv("ADMIN_TRUSTED_EDGE_CIDRS", TEST_TRUSTED_EDGE_CIDRS)
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    with shared_rate_limiter(rate_limit_store):
        headers = {
            "X-Test-Client-Host": TEST_RENDER_PEER,
            "X-Forwarded-For": f"203.0.113.77, {TEST_CF_EDGE}",
        }
        assert _login(username="ghost", password="wrong", headers=headers).status_code == 401
        assert _login(username="ghost", password="wrong", headers=headers).status_code == 401
        assert _login(username="ghost", password="wrong", headers=headers).status_code == 429

        other_ip_headers = {
            "X-Test-Client-Host": TEST_RENDER_PEER,
            "X-Forwarded-For": f"203.0.113.88, {TEST_CF_EDGE}",
        }
        assert _login(username="other-ghost", password="wrong", headers=other_ip_headers).status_code == 401


@pytest.mark.unit
@pytest.mark.integration
def test_rate_limit_ignores_spoofed_forwarded_without_trusted_peer(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", TEST_TRUSTED_PROXY_CIDRS)
    with shared_rate_limiter(rate_limit_store):
        for i in range(5):
            forwarded = f"203.0.113.{i}"
            response = _login(password="wrong", headers={"X-Forwarded-For": forwarded})
            assert response.status_code == 401

        blocked = _login(password="wrong", headers={"X-Forwarded-For": "203.0.113.99"})
        assert blocked.status_code == 429


@pytest.mark.unit
@pytest.mark.integration
def test_rotating_spoofed_forwarded_headers_share_trusted_peer_bucket(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "3")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", TEST_TRUSTED_PROXY_CIDRS)
    with shared_rate_limiter(rate_limit_store):
        for index in range(4):
            response = _login(
                username="ghost",
                password="wrong",
                headers={
                    "X-Test-Client-Host": TEST_RENDER_PEER,
                    "X-Forwarded-For": f"203.0.113.{index}",
                },
            )
            if index < 3:
                assert response.status_code == 401
            else:
                assert response.status_code == 429

    assert len(rate_limit_store.rows) == 2
    settings = get_settings()
    source_key = admin_auth.build_source_rate_limit_key(TEST_RENDER_PEER, settings)
    candidate_key = admin_auth.build_candidate_rate_limit_key("ghost", settings)
    assert source_key in rate_limit_store.rows
    assert candidate_key in rate_limit_store.rows


@pytest.mark.unit
@pytest.mark.integration
def test_rate_limit_rows_contain_no_raw_forwarding_data(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", TEST_TRUSTED_PROXY_CIDRS)
    monkeypatch.setenv("ADMIN_TRUSTED_EDGE_CIDRS", TEST_TRUSTED_EDGE_CIDRS)
    with shared_rate_limiter(rate_limit_store):
        _login(
            password="wrong",
            headers={
                "X-Test-Client-Host": TEST_RENDER_PEER,
                "X-Forwarded-For": f"203.0.113.55, {TEST_CF_EDGE}",
            },
        )

    for key, row in rate_limit_store.rows.items():
        assert len(key) == 64
        assert "203.0.113" not in key
        assert "x-forwarded-for" not in str(row).lower()


@pytest.mark.unit
@pytest.mark.integration
def test_rate_limit_fallback_when_database_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "5")
    with (
        patch(
            "app.admin_auth.db.try_admit_admin_login",
            side_effect=Exception("database unavailable"),
        ),
        patch(
            "app.admin_auth.db.cleanup_expired_admin_login_rate_limits",
            side_effect=Exception("database unavailable"),
        ),
        mock_db_connection(),
    ):
        for _ in range(2):
            assert _login(password="wrong").status_code == 401

        blocked = _login(password="wrong")
        assert blocked.status_code == 429
        assert admin_auth.LOGIN_THROTTLED_MESSAGE in blocked.text


@pytest.mark.unit
@pytest.mark.integration
def test_username_rotation_stops_password_verification_at_source_threshold(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "3")
    verify_calls = {"count": 0}
    original_verify = admin_auth.verify_admin_credentials

    def counting_verify(username: str, password: str, settings: Any) -> bool:
        verify_calls["count"] += 1
        return original_verify(username, password, settings)

    with shared_rate_limiter(rate_limit_store):
        with (
            mock_db_connection(),
            patch(
                "app.admin_routes.admin_auth.verify_admin_credentials",
                side_effect=counting_verify,
            ),
        ):
            for index in range(4):
                response = _login(username=f"user-{index}", password="wrong")
                if index < 3:
                    assert response.status_code == 401
                else:
                    assert response.status_code == 429

    assert verify_calls["count"] == 3
    settings = get_settings()
    source_key = admin_auth.build_source_rate_limit_key("testclient", settings)
    assert len(rate_limit_store.rows) == 5
    assert source_key in rate_limit_store.rows


@pytest.mark.unit
@pytest.mark.integration
def test_already_locked_requests_skip_password_verification_and_audit_amplification(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    verify_calls = {"count": 0}

    with shared_rate_limiter(rate_limit_store):
        with (
            mock_db_connection(),
            patch(
                "app.admin_routes.admin_auth.verify_admin_credentials",
                side_effect=lambda *_args, **_kwargs: verify_calls.__setitem__(
                    "count", verify_calls["count"] + 1
                )
                or False,
            ),
            patch("app.admin_routes._record_login_failure") as audit_mock,
        ):
            assert _login(password="wrong").status_code == 401
            assert _login(password="wrong").status_code == 401
            assert verify_calls["count"] == 2
            audit_calls_after_lock = audit_mock.call_count

            for _ in range(5):
                blocked = _login(password="wrong")
                assert blocked.status_code == 429

    assert verify_calls["count"] == 2
    assert audit_mock.call_count == audit_calls_after_lock


@pytest.mark.unit
@pytest.mark.integration
def test_lockout_transition_records_single_audit_event(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection(), patch("app.admin_routes._record_login_failure") as audit_mock:
            assert _login(password="wrong").status_code == 401
            assert audit_mock.call_count == 1

            lockout = _login(password="wrong")
            assert lockout.status_code == 401
            assert audit_mock.call_count == 2
            assert audit_mock.call_args_list[-1].kwargs["reason"] == "rate_limited"


@pytest.mark.unit
@pytest.mark.integration
def test_oversized_login_fields_rejected_before_password_verification(
    rate_limit_store: FakeRateLimitStore,
) -> None:
    verify_calls = {"count": 0}
    with shared_rate_limiter(rate_limit_store):
        with (
            mock_db_connection(),
            patch(
                "app.admin_routes.admin_auth.verify_admin_credentials",
                side_effect=lambda *_args, **_kwargs: verify_calls.__setitem__(
                    "count", verify_calls["count"] + 1
                )
                or False,
            ),
        ):
            csrf_token, cookies = _fetch_login_form()
            huge = "x" * (admin_auth.LOGIN_USERNAME_MAX_LENGTH + 1)
            response = client.post(
                "/admin/login",
                data={
                    "username": huge,
                    "password": TEST_PASSWORD,
                    "csrf_token": csrf_token,
                },
                cookies=cookies,
            )
    assert response.status_code == 400
    assert admin_auth.INVALID_CREDENTIALS_MESSAGE in response.text
    assert verify_calls["count"] == 0
    assert _login_flow_set_cookie_headers(response) == []


@pytest.mark.unit
@pytest.mark.integration
def test_concurrent_login_admission_respects_shared_threshold(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "5")
    barrier = threading.Barrier(8)
    admitted_count = {"value": 0}
    lock = threading.Lock()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    settings = get_settings()
    source_key = admin_auth.build_source_rate_limit_key("203.0.113.77", settings)

    def worker() -> None:
        barrier.wait()
        admission = rate_limit_store.try_admit(
            (source_key,),
            now,
            rate_limit=5,
            window_seconds=900,
            lockout_seconds=900,
        )
        if admission.admitted:
            with lock:
                admitted_count["value"] += 1

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert admitted_count["value"] == 5


@pytest.mark.unit
@pytest.mark.integration
def test_candidate_rate_limit_blocks_any_username_across_sources(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", TEST_TRUSTED_PROXY_CIDRS)
    monkeypatch.setenv("ADMIN_TRUSTED_EDGE_CIDRS", TEST_TRUSTED_EDGE_CIDRS)
    with shared_rate_limiter(rate_limit_store):
        assert _login(
            password="wrong",
            headers={
                "X-Test-Client-Host": TEST_RENDER_PEER,
                "X-Forwarded-For": f"203.0.113.1, {TEST_CF_EDGE}",
            },
        ).status_code == 401
        assert _login(
            password="wrong",
            headers={
                "X-Test-Client-Host": TEST_RENDER_PEER,
                "X-Forwarded-For": f"203.0.113.2, {TEST_CF_EDGE}",
            },
        ).status_code == 401
        blocked = _login(
            password="wrong",
            headers={
                "X-Test-Client-Host": TEST_RENDER_PEER,
                "X-Forwarded-For": f"203.0.113.3, {TEST_CF_EDGE}",
            },
        )
    assert blocked.status_code == 429


@pytest.mark.unit
@pytest.mark.integration
def test_candidate_rate_limit_blocks_unknown_username_across_sources(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", TEST_TRUSTED_PROXY_CIDRS)
    monkeypatch.setenv("ADMIN_TRUSTED_EDGE_CIDRS", TEST_TRUSTED_EDGE_CIDRS)
    with shared_rate_limiter(rate_limit_store):
        assert _login(
            username="ghost",
            password="wrong",
            headers={
                "X-Test-Client-Host": TEST_RENDER_PEER,
                "X-Forwarded-For": f"203.0.113.1, {TEST_CF_EDGE}",
            },
        ).status_code == 401
        assert _login(
            username="ghost",
            password="wrong",
            headers={
                "X-Test-Client-Host": TEST_RENDER_PEER,
                "X-Forwarded-For": f"203.0.113.2, {TEST_CF_EDGE}",
            },
        ).status_code == 401
        blocked = _login(
            username="ghost",
            password="wrong",
            headers={
                "X-Test-Client-Host": TEST_RENDER_PEER,
                "X-Forwarded-For": f"203.0.113.3, {TEST_CF_EDGE}",
            },
        )
    assert blocked.status_code == 429


@pytest.mark.unit
@pytest.mark.integration
def test_expired_session_redirects_to_login() -> None:
    raw_token = admin_auth.generate_session_token()
    token_hash = admin_auth.hash_session_token(raw_token)
    _session_store[token_hash] = _session_row(
        token_hash=token_hash,
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    with mock_db_connection():
        response = client.get("/admin", cookies={SESSION_COOKIE_NAME: raw_token})
    assert response.status_code == 303
    assert response.headers["location"].startswith("/admin/login")


@pytest.mark.unit
@pytest.mark.integration
def test_revoked_session_redirects_to_login() -> None:
    raw_token = admin_auth.generate_session_token()
    token_hash = admin_auth.hash_session_token(raw_token)
    _session_store[token_hash] = _session_row(
        token_hash=token_hash,
        revoked_at=datetime.now(timezone.utc),
    )
    with mock_db_connection():
        response = client.get("/admin", cookies={SESSION_COOKIE_NAME: raw_token})
    assert response.status_code == 303


@pytest.mark.unit
@pytest.mark.integration
def test_login_regenerates_session_and_revokes_prior_cookie(
    rate_limit_store: FakeRateLimitStore,
) -> None:
    old_token = admin_auth.generate_session_token()
    old_hash = admin_auth.hash_session_token(old_token)
    _session_store[old_hash] = _session_row(token_hash=old_hash, session_id=1)

    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            csrf_token, flow_cookies = _parse_login_form(client.get("/admin/login"))
            flow_cookies[SESSION_COOKIE_NAME] = old_token
            response = client.post(
                "/admin/login",
                data={
                    "username": TEST_USERNAME,
                    "password": TEST_PASSWORD,
                    "csrf_token": csrf_token,
                },
                cookies=flow_cookies,
            )
            assert response.status_code == 303
            assert _session_store[old_hash]["revoked_at"] is not None
            new_cookie = _extract_session_cookie(response)
            assert new_cookie
            assert new_cookie != old_token


@pytest.mark.unit
def test_issue_session_uses_single_connection_and_transaction() -> None:
    conn = MagicMock()
    db_calls = 0

    @contextmanager
    def counting_connection(database_url: str | None) -> Generator[MagicMock, None, None]:
        nonlocal db_calls
        db_calls += 1
        yield conn

    request = MagicMock(spec=Request)
    response = RedirectResponse(url="/admin", status_code=303)
    settings = get_settings()

    with patch("app.admin_routes.db.db_connection", side_effect=counting_connection):
        with patch("app.admin_routes.crm_transaction", wraps=crm_transaction) as tx:
            with patch("app.admin_routes.db.revoke_admin_session") as revoke:
                with patch("app.admin_routes.db.create_admin_session", return_value=42):
                    with patch("app.admin_routes.audit_service.record_login_success"):
                        session_id = _issue_session(
                            request=request,
                            response=response,
                            settings=settings,
                            admin_username=TEST_USERNAME,
                            prior_raw_token="prior-session-token",
                        )

    assert session_id == 42
    assert db_calls == 1
    tx.assert_called_once()
    revoke.assert_called_once()


@pytest.mark.unit
def test_issue_session_without_prior_token_uses_single_transaction() -> None:
    conn = MagicMock()
    db_calls = 0

    @contextmanager
    def counting_connection(database_url: str | None) -> Generator[MagicMock, None, None]:
        nonlocal db_calls
        db_calls += 1
        yield conn

    request = MagicMock(spec=Request)
    response = RedirectResponse(url="/admin", status_code=303)
    settings = get_settings()

    with patch("app.admin_routes.db.db_connection", side_effect=counting_connection):
        with patch("app.admin_routes.crm_transaction", wraps=crm_transaction) as tx:
            with patch("app.admin_routes.db.revoke_admin_session") as revoke:
                with patch("app.admin_routes.db.create_admin_session", return_value=7):
                    with patch("app.admin_routes.audit_service.record_login_success"):
                        session_id = _issue_session(
                            request=request,
                            response=response,
                            settings=settings,
                            admin_username=TEST_USERNAME,
                            prior_raw_token=None,
                        )

    assert session_id == 7
    assert db_calls == 1
    tx.assert_called_once()
    revoke.assert_not_called()


@pytest.mark.unit
def test_issue_session_does_not_set_cookie_when_transaction_fails() -> None:
    conn = MagicMock()

    @contextmanager
    def fake_connection(database_url: str | None) -> Generator[MagicMock, None, None]:
        yield conn

    request = MagicMock(spec=Request)
    response = RedirectResponse(url="/admin", status_code=303)
    settings = get_settings()

    with patch("app.admin_routes.db.db_connection", side_effect=fake_connection):
        with patch(
            "app.admin_routes.audit_service.record_login_success",
            side_effect=RuntimeError("audit insert failed"),
        ):
            with pytest.raises(RuntimeError, match="audit insert failed"):
                _issue_session(
                    request=request,
                    response=response,
                    settings=settings,
                    admin_username=TEST_USERNAME,
                    prior_raw_token=None,
                )

    assert SESSION_COOKIE_NAME not in response.headers.get("set-cookie", "")


@pytest.mark.unit
@pytest.mark.integration
def test_login_session_create_failure_rolls_back_prior_revocation(
    rate_limit_store: FakeRateLimitStore,
) -> None:
    old_token = admin_auth.generate_session_token()
    old_hash = admin_auth.hash_session_token(old_token)
    _session_store[old_hash] = _session_row(token_hash=old_hash, session_id=1)
    initial_store_size = len(_session_store)

    def failing_create(conn: MagicMock, **kwargs: Any) -> int:
        raise RuntimeError("session insert failed")

    with shared_rate_limiter(rate_limit_store):
        with transactional_mock_db_connection():
            with patch("app.admin_routes.db.create_admin_session", side_effect=failing_create):
                with pytest.raises(RuntimeError, match="session insert failed"):
                    csrf_token, flow_cookies = _parse_login_form(client.get("/admin/login"))
                    flow_cookies[SESSION_COOKIE_NAME] = old_token
                    response = client.post(
                        "/admin/login",
                        data={
                            "username": TEST_USERNAME,
                            "password": TEST_PASSWORD,
                            "csrf_token": csrf_token,
                        },
                        cookies=flow_cookies,
                    )
                    assert SESSION_COOKIE_NAME not in response.cookies

    assert _session_store[old_hash]["revoked_at"] is None
    assert len(_session_store) == initial_store_size


@pytest.mark.unit
@pytest.mark.integration
def test_login_audit_failure_rolls_back_session_and_prior_revocation(
    rate_limit_store: FakeRateLimitStore,
) -> None:
    old_token = admin_auth.generate_session_token()
    old_hash = admin_auth.hash_session_token(old_token)
    _session_store[old_hash] = _session_row(token_hash=old_hash, session_id=1)
    initial_store_size = len(_session_store)

    with shared_rate_limiter(rate_limit_store):
        with transactional_mock_db_connection():
            with patch(
                "app.admin_routes.audit_service.record_login_success",
                side_effect=RuntimeError("audit insert failed"),
            ):
                with pytest.raises(RuntimeError, match="audit insert failed"):
                    csrf_token, flow_cookies = _parse_login_form(client.get("/admin/login"))
                    flow_cookies[SESSION_COOKIE_NAME] = old_token
                    response = client.post(
                        "/admin/login",
                        data={
                            "username": TEST_USERNAME,
                            "password": TEST_PASSWORD,
                            "csrf_token": csrf_token,
                        },
                        cookies=flow_cookies,
                    )
                    assert SESSION_COOKIE_NAME not in response.cookies

    assert _session_store[old_hash]["revoked_at"] is None
    assert len(_session_store) == initial_store_size


@pytest.mark.unit
@pytest.mark.integration
def test_login_retry_after_transaction_failure(
    rate_limit_store: FakeRateLimitStore,
) -> None:
    old_token = admin_auth.generate_session_token()
    old_hash = admin_auth.hash_session_token(old_token)
    _session_store[old_hash] = _session_row(token_hash=old_hash, session_id=1)
    attempts = {"count": 0}

    def flaky_create(conn: MagicMock, **kwargs: Any) -> int:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("transient session error")
        return _mock_create_admin_session(conn, **kwargs)

    with shared_rate_limiter(rate_limit_store):
        with transactional_mock_db_connection():
            with patch("app.admin_routes.db.create_admin_session", side_effect=flaky_create):
                with pytest.raises(RuntimeError, match="transient session error"):
                    csrf_token, flow_cookies = _parse_login_form(client.get("/admin/login"))
                    flow_cookies[SESSION_COOKIE_NAME] = old_token
                    client.post(
                        "/admin/login",
                        data={
                            "username": TEST_USERNAME,
                            "password": TEST_PASSWORD,
                            "csrf_token": csrf_token,
                        },
                        cookies=flow_cookies,
                    )

                assert _session_store[old_hash]["revoked_at"] is None

                csrf_token, flow_cookies = _parse_login_form(client.get("/admin/login"))
                flow_cookies[SESSION_COOKIE_NAME] = old_token
                response = client.post(
                    "/admin/login",
                    data={
                        "username": TEST_USERNAME,
                        "password": TEST_PASSWORD,
                        "csrf_token": csrf_token,
                    },
                    cookies=flow_cookies,
                )

    assert response.status_code == 303
    assert _session_store[old_hash]["revoked_at"] is not None
    new_cookie = _extract_session_cookie(response)
    assert new_cookie
    assert new_cookie != old_token


@pytest.mark.unit
@pytest.mark.integration
def test_login_two_tab_session_replacement_is_atomic(
    rate_limit_store: FakeRateLimitStore,
) -> None:
    """Two tabs sharing a session cookie: replacement revokes old and issues one new cookie."""
    shared_token = admin_auth.generate_session_token()
    shared_hash = admin_auth.hash_session_token(shared_token)
    _session_store[shared_hash] = _session_row(token_hash=shared_hash, session_id=1)

    with shared_rate_limiter(rate_limit_store):
        with transactional_mock_db_connection():
            csrf_a, cookies_a = _parse_login_form(client.get("/admin/login"))
            cookies_a[SESSION_COOKIE_NAME] = shared_token
            csrf_b, cookies_b = _parse_login_form(client.get("/admin/login"))
            cookies_b[SESSION_COOKIE_NAME] = shared_token

            response_a = client.post(
                "/admin/login",
                data={
                    "username": TEST_USERNAME,
                    "password": TEST_PASSWORD,
                    "csrf_token": csrf_a,
                },
                cookies=cookies_a,
            )
            response_b = client.post(
                "/admin/login",
                data={
                    "username": TEST_USERNAME,
                    "password": TEST_PASSWORD,
                    "csrf_token": csrf_b,
                },
                cookies=cookies_b,
            )

    assert response_a.status_code == 303
    assert response_b.status_code == 303
    assert _session_store[shared_hash]["revoked_at"] is not None
    cookie_a = _extract_session_cookie(response_a)
    cookie_b = _extract_session_cookie(response_b)
    assert cookie_a
    assert cookie_b
    assert cookie_a != shared_token
    assert cookie_b != shared_token


@pytest.mark.unit
@pytest.mark.integration
def test_session_cookie_flags(rate_limit_store: FakeRateLimitStore) -> None:
    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            response = _login()
    set_cookie = response.headers.get("set-cookie", "")
    assert "HttpOnly" in set_cookie
    assert "Path=/admin" in set_cookie
    assert "SameSite=strict" in set_cookie


@pytest.mark.unit
@pytest.mark.integration
def test_login_flow_cookie_flags() -> None:
    with mock_db_connection():
        response = client.get("/admin/login")
    set_cookie = response.headers.get("set-cookie", "")
    assert "HttpOnly" in set_cookie
    assert "Path=/admin" in set_cookie
    assert "SameSite=strict" in set_cookie


@pytest.mark.unit
@pytest.mark.integration
def test_login_honors_safe_next_redirect(rate_limit_store: FakeRateLimitStore) -> None:
    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            response = _login(next_path="/admin")
    assert response.status_code == 303
    assert response.headers["location"] == "/admin"


@pytest.mark.unit
@pytest.mark.integration
def test_login_ignores_external_next_redirect(rate_limit_store: FakeRateLimitStore) -> None:
    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            response = _login(next_path="https://evil.example/phish")
    assert response.headers["location"] == "/admin"


@pytest.mark.unit
def test_derive_session_csrf_token_is_stable_for_session() -> None:
    settings = get_settings()
    raw = admin_auth.generate_session_token()
    first = admin_auth.derive_session_csrf_token(raw, settings)
    second = admin_auth.derive_session_csrf_token(raw, settings)
    assert first == second
    assert len(first) >= 32


@pytest.mark.unit
def test_derive_session_csrf_token_differs_across_sessions() -> None:
    settings = get_settings()
    token_a = admin_auth.generate_session_token()
    token_b = admin_auth.generate_session_token()
    assert admin_auth.derive_session_csrf_token(token_a, settings) != (
        admin_auth.derive_session_csrf_token(token_b, settings)
    )


@pytest.mark.unit
@pytest.mark.integration
def test_logout_rejects_invalid_session_csrf() -> None:
    with mock_db_connection():
        login = _login()
        session_cookie = _extract_session_cookie(login)
        assert session_cookie

        response = client.post(
            "/admin/logout",
            data={"csrf_token": "not-the-right-token"},
            cookies={SESSION_COOKIE_NAME: session_cookie},
        )
    assert response.status_code == 400
    assert admin_auth.INVALID_REQUEST_MESSAGE in response.json()["detail"]


@pytest.mark.unit
@pytest.mark.integration
def test_session_csrf_stable_across_navigation() -> None:
    """Opening another admin page must not invalidate forms already open."""
    with mock_db_connection():
        login = _login()
        session_cookie = _extract_session_cookie(login)
        assert session_cookie

        contacts = client.get("/admin/briefs", cookies={SESSION_COOKIE_NAME: session_cookie})
        csrf_from_briefs = _extract_csrf_token(contacts.text)

        dashboard = client.get("/admin", cookies={SESSION_COOKIE_NAME: session_cookie})
        csrf_from_dashboard = _extract_csrf_token(dashboard.text)

    assert csrf_from_briefs == csrf_from_dashboard

    with mock_db_connection():
        response = client.post(
            "/admin/logout",
            data={"csrf_token": csrf_from_briefs},
            cookies={SESSION_COOKIE_NAME: session_cookie},
        )
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"


@pytest.mark.unit
@pytest.mark.integration
def test_two_tab_form_stays_valid_after_other_tab_navigation(
    rate_limit_store: FakeRateLimitStore,
) -> None:
    """Simulate tab A form left open while tab B navigates elsewhere."""
    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            login = _login()
            session_cookie = _extract_session_cookie(login)
            assert session_cookie

            tab_a = client.get("/admin/briefs", cookies={SESSION_COOKIE_NAME: session_cookie})
            tab_a_csrf = _extract_csrf_token(tab_a.text)

            client.get("/admin", cookies={SESSION_COOKIE_NAME: session_cookie})
            client.get("/admin/audit", cookies={SESSION_COOKIE_NAME: session_cookie})

            logout = client.post(
                "/admin/logout",
                data={"csrf_token": tab_a_csrf},
                cookies={SESSION_COOKIE_NAME: session_cookie},
            )
    assert logout.status_code == 303


@pytest.mark.unit
@pytest.mark.integration
def test_session_csrf_rejects_cross_session_token() -> None:
    settings = get_settings()
    raw_a = admin_auth.generate_session_token()
    raw_b = admin_auth.generate_session_token()
    csrf_a = admin_auth.derive_session_csrf_token(raw_a, settings)
    _session_store[admin_auth.hash_session_token(raw_b)] = _session_row(
        token_hash=admin_auth.hash_session_token(raw_b),
        session_id=2,
    )

    with mock_db_connection():
        response = client.post(
            "/admin/logout",
            data={"csrf_token": csrf_a},
            cookies={SESSION_COOKIE_NAME: raw_b},
        )
    assert response.status_code == 400
    assert admin_auth.INVALID_REQUEST_MESSAGE in response.json()["detail"]


@pytest.mark.unit
@pytest.mark.integration
def test_session_csrf_rejects_malformed_token() -> None:
    with mock_db_connection():
        login = _login()
        session_cookie = _extract_session_cookie(login)
        assert session_cookie

        response = client.post(
            "/admin/logout",
            data={"csrf_token": "not-a-valid-token"},
            cookies={SESSION_COOKIE_NAME: session_cookie},
        )
    assert response.status_code == 400


@pytest.mark.unit
@pytest.mark.integration
def test_expired_session_csrf_rejected_on_protected_get() -> None:
    raw_token = admin_auth.generate_session_token()
    token_hash = admin_auth.hash_session_token(raw_token)
    _session_store[token_hash] = _session_row(
        token_hash=token_hash,
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    with mock_db_connection():
        response = client.get("/admin", cookies={SESSION_COOKIE_NAME: raw_token})
    assert response.status_code == 303
    assert response.headers["location"].startswith("/admin/login")


@pytest.mark.unit
@pytest.mark.integration
def test_concurrent_session_csrf_submissions() -> None:
    """The same valid token may be submitted concurrently until the session ends."""
    with mock_db_connection():
        login = _login()
        session_cookie = _extract_session_cookie(login)
        assert session_cookie

        dashboard = client.get("/admin", cookies={SESSION_COOKIE_NAME: session_cookie})
        csrf_token = _extract_csrf_token(dashboard.text)

        first = client.post(
            "/admin/logout",
            data={"csrf_token": csrf_token},
            cookies={SESSION_COOKIE_NAME: session_cookie},
        )
        second = client.post(
            "/admin/logout",
            data={"csrf_token": csrf_token},
            cookies={SESSION_COOKIE_NAME: session_cookie},
        )
    assert first.status_code == 303
    assert second.status_code == 303
    assert second.headers["location"] == "/admin/login"


@pytest.mark.unit
@pytest.mark.integration
def test_login_replaces_session_csrf_token() -> None:
    """A new login invalidates CSRF tokens from the replaced session cookie."""
    settings = get_settings()
    old_token = admin_auth.generate_session_token()
    old_csrf = admin_auth.derive_session_csrf_token(old_token, settings)
    _session_store[admin_auth.hash_session_token(old_token)] = _session_row(
        token_hash=admin_auth.hash_session_token(old_token),
    )

    with mock_db_connection():
        response = client.post(
            "/admin/logout",
            data={"csrf_token": old_csrf},
            cookies={SESSION_COOKIE_NAME: old_token},
        )
    assert response.status_code == 303

    with mock_db_connection():
        login = _login()
        new_cookie = _extract_session_cookie(login)
        assert new_cookie
        assert new_cookie != old_token
        new_csrf = admin_auth.derive_session_csrf_token(new_cookie, settings)
        assert new_csrf != old_csrf

        rejected = client.post(
            "/admin/logout",
            data={"csrf_token": old_csrf},
            cookies={SESSION_COOKIE_NAME: new_cookie},
        )
    assert rejected.status_code == 400


@pytest.mark.unit
@pytest.mark.integration
def test_logout_accepts_stable_csrf_after_refresh() -> None:
    with mock_db_connection():
        login = _login()
        session_cookie = _extract_session_cookie(login)
        assert session_cookie

        dashboard_a = client.get("/admin", cookies={SESSION_COOKIE_NAME: session_cookie})
        csrf_a = _extract_csrf_token(dashboard_a.text)

        dashboard_b = client.get("/admin", cookies={SESSION_COOKIE_NAME: session_cookie})
        csrf_b = _extract_csrf_token(dashboard_b.text)

        assert csrf_a == csrf_b

        response = client.post(
            "/admin/logout",
            data={"csrf_token": csrf_a},
            cookies={SESSION_COOKIE_NAME: session_cookie},
        )
    assert response.status_code == 303


@pytest.mark.unit
@pytest.mark.integration
def test_revoked_session_logout_csrf_fails_closed() -> None:
    raw_token = admin_auth.generate_session_token()
    token_hash = admin_auth.hash_session_token(raw_token)
    csrf_hash = admin_auth.hash_csrf_token(admin_auth.generate_csrf_value())
    _session_store[token_hash] = _session_row(
        token_hash=token_hash,
        csrf_token_hash=csrf_hash,
        revoked_at=datetime.now(timezone.utc),
    )
    with mock_db_connection():
        response = client.post(
            "/admin/logout",
            data={"csrf_token": admin_auth.generate_csrf_value()},
            cookies={SESSION_COOKIE_NAME: raw_token},
        )
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"


@pytest.mark.unit
@pytest.mark.integration
def test_no_registration_or_reset_routes_exist() -> None:
    with mock_db_connection():
        for path in (
            "/admin/register",
            "/admin/signup",
            "/admin/password-reset",
            "/admin/forgot-password",
        ):
            response = client.get(path)
            assert response.status_code == 303
            assert response.headers["location"].startswith("/admin/login")


@pytest.mark.unit
@pytest.mark.integration
def test_admin_unconfigured_returns_service_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ADMIN_USERNAME", raising=False)
    response = client.get("/admin/login")
    assert response.status_code == 503


@pytest.mark.unit
@pytest.mark.integration
def test_login_flow_cleanup_runs_when_minting_new_flow() -> None:
    now = datetime.now(timezone.utc)
    stale_hash = admin_auth.hash_session_token("stale-flow-token")
    _login_flows[stale_hash] = {
        "id": 99,
        "flow_token_hash": stale_hash,
        "csrf_token_hash": "stale-csrf",
        "created_at": now - timedelta(hours=2),
        "expires_at": now - timedelta(seconds=admin_auth.LOGIN_FLOW_EXPIRED_RETENTION_SECONDS + 1),
        "consumed_at": None,
    }
    active_hash = admin_auth.hash_session_token("active-flow-token")
    _login_flows[active_hash] = {
        "id": 100,
        "flow_token_hash": active_hash,
        "csrf_token_hash": "active-csrf",
        "created_at": now,
        "expires_at": now + timedelta(minutes=10),
        "consumed_at": None,
    }

    with mock_db_connection():
        response = client.get("/admin/login")

    assert response.status_code == 200
    assert stale_hash not in _login_flows
    assert active_hash in _login_flows
    assert len(_login_flows) == 2


@pytest.mark.unit
@pytest.mark.integration
def test_login_flow_cleanup_failure_still_mints_flow() -> None:
    with mock_db_connection():
        with patch(
            "app.admin_routes.db.cleanup_stale_admin_login_flows",
            side_effect=Exception("database unavailable"),
        ):
            response = client.get("/admin/login")

    assert response.status_code == 200
    assert "Admin sign in" in response.text
    assert response.cookies.get(LOGIN_FLOW_COOKIE_NAME)
    assert len(_login_flows) == 1


@pytest.mark.unit
@pytest.mark.integration
def test_login_flow_cleanup_failure_retry_succeeds(rate_limit_store: FakeRateLimitStore) -> None:
    cleanup_calls = {"count": 0}

    def flaky_cleanup(conn: Any, **kwargs: Any) -> int:
        cleanup_calls["count"] += 1
        if cleanup_calls["count"] == 1:
            raise Exception("transient database error")
        return _mock_cleanup_stale_admin_login_flows(conn, **kwargs)

    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            with patch(
                "app.admin_routes.db.cleanup_stale_admin_login_flows",
                side_effect=flaky_cleanup,
            ):
                first = client.get("/admin/login")
                assert first.status_code == 200

                second = client.get("/admin/login")
                assert second.status_code == 200

    assert cleanup_calls["count"] == 2
