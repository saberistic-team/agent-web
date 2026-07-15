"""Integration tests for admin login proxy trust through Uvicorn."""

from __future__ import annotations

import re
import socket
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Generator
from unittest.mock import MagicMock, patch

import httpx
import pytest
import uvicorn
from argon2 import PasswordHasher

from app import admin_auth
from app.admin_auth import LOGIN_FLOW_COOKIE_NAME
from app.main import app

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "test-session-secret-32chars-minimum"

TRUSTED_PROXY_CIDRS = "127.0.0.1,10.0.0.0/8"
EDGE_PROXY_CIDRS = "198.51.100.0/24"

_login_flows: dict[str, dict[str, Any]] = {}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _mock_create_admin_login_flow(conn: MagicMock, **kwargs: Any) -> int:
    flow_hash = kwargs["flow_token_hash"]
    _login_flows[flow_hash] = {
        "id": len(_login_flows) + 1,
        "flow_token_hash": flow_hash,
        "csrf_token_hash": kwargs["csrf_token_hash"],
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
    if row.get("csrf_token_hash") != csrf_token_hash:
        return None
    expires_at = row["expires_at"]
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= now:
        return None
    row["consumed_at"] = now
    return dict(row)


class _FakeRateLimitStore:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    def try_admit(self, limiter_keys: tuple[str, ...], *, rate_limit: int) -> bool:
        for limiter_key in limiter_keys:
            row = self.rows.setdefault(
                limiter_key,
                {"failure_count": 0, "locked_until": None},
            )
            if row["locked_until"] is not None:
                return False
        for limiter_key in limiter_keys:
            row = self.rows[limiter_key]
            row["failure_count"] += 1
            if row["failure_count"] >= rate_limit:
                row["locked_until"] = True
        return True


@contextmanager
def _patched_login_backend(store: _FakeRateLimitStore) -> Generator[None, None, None]:
    from app import db

    def try_admit(
        conn: Any,
        *,
        limiter_keys: tuple[str, ...],
        now: Any,
        rate_limit: int,
        window_seconds: int,
        lockout_seconds: int,
    ) -> db.AdminLoginAdmission:
        admitted = store.try_admit(limiter_keys, rate_limit=rate_limit)
        return db.AdminLoginAdmission(
            admitted=admitted,
            throttled=not admitted,
            already_locked=not admitted,
            lockout_transition=False,
        )

    conn = MagicMock()
    with (
        patch("app.main.db.init_db"),
        patch("app.admin_auth.db.try_admit_admin_login", side_effect=try_admit),
        patch(
            "app.admin_auth.db.cleanup_expired_admin_login_rate_limits",
            return_value=0,
        ),
        patch("app.admin_auth.db.db_connection") as admin_db_conn,
        patch("app.admin_routes.db.db_connection") as routes_db_conn,
        patch(
            "app.admin_routes.db.create_admin_login_flow",
            side_effect=_mock_create_admin_login_flow,
        ),
        patch(
            "app.admin_routes.db.cleanup_stale_admin_login_flows",
            return_value=0,
        ),
        patch(
            "app.admin_routes.db.claim_admin_login_flow",
            side_effect=_mock_claim_admin_login_flow,
        ),
    ):
        admin_db_conn.return_value.__enter__.return_value = conn
        admin_db_conn.return_value.__exit__.return_value = None
        routes_db_conn.return_value.__enter__.return_value = conn
        routes_db_conn.return_value.__exit__.return_value = None
        _login_flows.clear()
        admin_auth.reset_login_rate_limiter()
        yield


@contextmanager
def _uvicorn_server(
    monkeypatch: pytest.MonkeyPatch,
    *,
    trusted_cidrs: str = TRUSTED_PROXY_CIDRS,
    edge_cidrs: str = EDGE_PROXY_CIDRS,
) -> Generator[str, None, None]:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", trusted_cidrs)
    monkeypatch.setenv("ADMIN_EDGE_PROXY_CIDRS", edge_cidrs)

    port = _free_port()
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        proxy_headers=True,
        forwarded_allow_ips=trusted_cidrs,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    origin = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            with httpx.Client() as client:
                if client.get(f"{origin}/health", timeout=0.5).status_code == 200:
                    break
        except httpx.HTTPError:
            time.sleep(0.05)
    else:
        server.should_exit = True
        thread.join(timeout=2)
        pytest.fail("uvicorn server did not become ready")

    try:
        yield origin
    finally:
        server.should_exit = True
        thread.join(timeout=2)


def _login(
    client: httpx.Client,
    origin: str,
    *,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    form = client.get(f"{origin}/admin/login")
    assert form.status_code == 200
    match = re.search(r'name="csrf_token" value="([^"]+)"', form.text)
    assert match is not None
    cookies = {
        LOGIN_FLOW_COOKIE_NAME: form.cookies.get(LOGIN_FLOW_COOKIE_NAME, ""),
    }
    return client.post(
        f"{origin}/admin/login",
        data={
            "username": "ghost",
            "password": "wrong",
            "csrf_token": match.group(1),
        },
        cookies=cookies,
        headers=headers or {},
    )


@pytest.mark.integration
def test_uvicorn_trusted_proxy_rotating_spoofed_headers_share_one_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _FakeRateLimitStore()
    with (
        _patched_login_backend(store),
        _uvicorn_server(monkeypatch) as origin,
    ):
        real_client = "203.0.113.77"
        headers = {"X-Forwarded-For": f"203.0.113.50, {real_client}, 127.0.0.1"}
        with httpx.Client() as client:
            assert _login(client, origin, headers=headers).status_code == 401
            assert _login(client, origin, headers=headers).status_code == 401
            blocked = _login(client, origin, headers=headers)
            assert blocked.status_code == 429

            rotated = {"X-Forwarded-For": f"203.0.113.99, {real_client}, 127.0.0.1"}
            assert _login(client, origin, headers=rotated).status_code == 429

    source_key = admin_auth.build_source_rate_limit_key(real_client)
    assert len(store.rows) == 1
    assert source_key in store.rows


@pytest.mark.integration
def test_uvicorn_untrusted_peer_ignores_forwarding_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _FakeRateLimitStore()
    with (
        _patched_login_backend(store),
        _uvicorn_server(monkeypatch, trusted_cidrs="10.0.0.0/8") as origin,
    ):
        with httpx.Client() as client:
            for index in range(2):
                headers = {"X-Forwarded-For": f"203.0.113.{index}"}
                assert _login(client, origin, headers=headers).status_code == 401
            blocked = _login(
                client,
                origin,
                headers={"X-Forwarded-For": "203.0.113.99"},
            )
            assert blocked.status_code == 429

    assert len(store.rows) == 1
    assert admin_auth.build_source_rate_limit_key("127.0.0.1") in store.rows
