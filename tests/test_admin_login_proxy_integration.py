"""Integration tests for admin login proxy trust via Uvicorn."""

from __future__ import annotations

import re
import socket
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Generator
from unittest.mock import MagicMock, patch

import httpx
import pytest
import uvicorn
from argon2 import PasswordHasher

from app import admin_auth, db
from app.main import app

TEST_USERNAME = "proxy-operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "test-session-secret-32chars-minimum"
TRUSTED_CIDRS = "127.0.0.0/8,10.0.0.0/8"
FORWARDED_ALLOW_IPS = "127.0.0.0/8,10.0.0.0/8"

_login_flows: dict[str, dict[str, Any]] = {}


class _InMemoryRateLimitStore:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    def try_admit(
        self,
        limiter_keys: tuple[str, ...],
        now: datetime,
        *,
        rate_limit: int,
        window_seconds: int,
        lockout_seconds: int,
    ) -> db.AdminLoginAdmission:
        for limiter_key in limiter_keys:
            row = self.rows.setdefault(
                limiter_key,
                {
                    "failure_count": 0,
                    "window_started_at": now,
                    "locked_until": None,
                    "updated_at": now,
                },
            )
            row["failure_count"] += 1
        return db.AdminLoginAdmission(
            admitted=True,
            throttled=False,
            already_locked=False,
            lockout_transition=False,
        )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextmanager
def _uvicorn_server() -> Generator[str, None, None]:
    port = _free_port()
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        forwarded_allow_ips=FORWARDED_ALLOW_IPS,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            if httpx.get(f"{base_url}/health", timeout=1.0).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.05)
    else:
        server.should_exit = True
        thread.join(timeout=2)
        raise RuntimeError("uvicorn server failed to start")
    try:
        yield base_url
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@pytest.fixture(autouse=True)
def proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", TRUSTED_CIDRS)
    admin_auth.reset_login_rate_limiter()
    _login_flows.clear()


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


def _mock_claim_admin_login_flow(
    conn: MagicMock,
    *,
    flow_token_hash: str,
    csrf_token_hash: str,
    now: datetime,
) -> dict[str, Any] | None:
    row = _login_flows.get(flow_token_hash)
    if row is None or row.get("consumed_at") is not None:
        return None
    expires_at = row["expires_at"]
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= now or row.get("csrf_token_hash") != csrf_token_hash:
        return None
    row["consumed_at"] = now
    return dict(row)


def _fetch_login_form(client: httpx.Client) -> tuple[str, dict[str, str]]:
    response = client.get("/admin/login")
    assert response.status_code == 200
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert match is not None
    return match.group(1), dict(response.cookies)


@pytest.mark.integration
def test_uvicorn_proxy_chain_ignores_spoofed_leftmost_xff() -> None:
    store = _InMemoryRateLimitStore()

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

    with (
        patch("app.main.db.init_db", return_value=None),
        _uvicorn_server() as base_url,
        patch("app.admin_auth.db.try_admit_admin_login", side_effect=try_admit),
        patch("app.admin_auth.db.cleanup_expired_admin_login_rate_limits", return_value=0),
        patch("app.admin_auth.db.db_connection") as db_conn,
        patch("app.admin_routes.db.create_admin_login_flow", _mock_create_admin_login_flow),
        patch("app.admin_routes.db.claim_admin_login_flow", _mock_claim_admin_login_flow),
        patch("app.admin_routes.db.cleanup_stale_admin_login_flows", return_value=0),
        patch("app.admin_routes.db.db_connection") as routes_db_conn,
    ):
        db_conn.return_value.__enter__.return_value = MagicMock()
        db_conn.return_value.__exit__.return_value = None
        routes_db_conn.return_value.__enter__.return_value = MagicMock()
        routes_db_conn.return_value.__exit__.return_value = None

        with httpx.Client(base_url=base_url, timeout=10.0) as client:
            csrf_token, cookies = _fetch_login_form(client)
            first = client.post(
                "/admin/login",
                data={
                    "username": "ghost",
                    "password": "wrong-password",
                    "csrf_token": csrf_token,
                },
                cookies=cookies,
                headers={"X-Forwarded-For": "203.0.113.99, 198.51.100.42"},
            )
            assert first.status_code == 401

            csrf_token, cookies = _fetch_login_form(client)
            second = client.post(
                "/admin/login",
                data={
                    "username": "ghost",
                    "password": "wrong-password",
                    "csrf_token": csrf_token,
                },
                cookies=cookies,
                headers={"X-Forwarded-For": "203.0.113.100, 198.51.100.42"},
            )
            assert second.status_code == 401

    expected_key = admin_auth.build_source_rate_limit_key("198.51.100.42")
    assert expected_key in store.rows
    assert admin_auth.build_source_rate_limit_key("203.0.113.99") not in store.rows
    assert admin_auth.build_source_rate_limit_key("203.0.113.100") not in store.rows
    assert store.rows[expected_key]["failure_count"] == 2


@pytest.mark.integration
def test_health_reports_admin_proxy_trust_configuration() -> None:
    with patch("app.main.db.init_db", return_value=None), _uvicorn_server() as base_url:
        response = httpx.get(f"{base_url}/health", timeout=5.0)
    payload = response.json()
    assert payload["admin_proxy_trust"] == {
        "enabled": True,
        "boundary_configured": True,
    }
