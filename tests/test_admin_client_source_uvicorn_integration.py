"""Uvicorn integration coverage for admin login proxy trust (#239)."""

from __future__ import annotations

import re
import threading
import time
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from typing import Any, Generator
from unittest.mock import MagicMock, patch

import httpx
import pytest
import uvicorn
from argon2 import PasswordHasher

from app import admin_auth

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "test-session-secret-32chars-minimum"

RENDER_PEER = "10.0.0.9"
RENDER_TRUSTED_CIDRS = "10.0.0.0/8"
REAL_CLIENT = "203.0.113.55"
SPOOFED_CLIENT = "203.0.113.66"

_login_flows: dict[str, dict[str, Any]] = {}


class _TrustedPeerProxy:
    """ASGI wrapper that rewrites the connecting peer to a trusted Render address."""

    def __init__(self, app: object, peer_host: str) -> None:
        self._app = app
        self._peer_host = peer_host

    async def __call__(self, scope: dict, receive: object, send: object) -> None:
        if scope["type"] == "http":
            scope = dict(scope)
            scope["client"] = (self._peer_host, 54321)
        await self._app(scope, receive, send)


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
    row["consumed_at"] = now
    return dict(row)


@contextmanager
def _uvicorn_server(monkeypatch: pytest.MonkeyPatch) -> Generator[str, None, None]:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", RENDER_TRUSTED_CIDRS)
    monkeypatch.delenv("ADMIN_CLOUDFLARE_PROXY_CIDRS", raising=False)
    admin_auth.reset_login_rate_limiter()
    _login_flows.clear()

    store: dict[str, int] = {}

    def fake_try_admit(
        conn: Any,
        *,
        limiter_keys: tuple[str, ...],
        now: datetime,
        rate_limit: int,
        window_seconds: int,
        lockout_seconds: int,
    ) -> Any:
        _ = (conn, now, window_seconds, lockout_seconds)
        admitted = True
        for key in limiter_keys:
            count = store.get(key, 0) + 1
            store[key] = count
            if count > rate_limit:
                admitted = False
        from app import db

        return db.AdminLoginAdmission(
            admitted=admitted,
            throttled=not admitted,
            already_locked=not admitted,
            lockout_transition=False,
        )

    from app.main import app

    with ExitStack() as stack:
        stack.enter_context(patch("app.db.init_db", lambda *_args, **_kwargs: None))
        stack.enter_context(
            patch("app.admin_routes.db.create_admin_login_flow", _mock_create_admin_login_flow)
        )
        stack.enter_context(
            patch("app.admin_routes.db.cleanup_stale_admin_login_flows", lambda *_a, **_k: 0)
        )
        stack.enter_context(
            patch("app.admin_routes.db.claim_admin_login_flow", _mock_claim_admin_login_flow)
        )
        stack.enter_context(patch("app.admin_auth.db.try_admit_admin_login", fake_try_admit))
        stack.enter_context(
            patch("app.admin_auth.db.cleanup_expired_admin_login_rate_limits", lambda *_a, **_k: 0)
        )
        conn_patch = stack.enter_context(patch("app.admin_auth.db.db_connection"))
        conn_patch.return_value.__enter__.return_value = MagicMock()
        conn_patch.return_value.__exit__.return_value = None

        from app.main import app

        wrapped = _TrustedPeerProxy(app, RENDER_PEER)
        config = uvicorn.Config(
            wrapped,
            host="127.0.0.1",
            port=0,
            log_level="warning",
            proxy_headers=False,
            forwarded_allow_ips=RENDER_TRUSTED_CIDRS,
        )
        server = uvicorn.Server(config)

        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()

        deadline = time.time() + 5
        while not server.started and time.time() < deadline:
            time.sleep(0.01)
        assert server.started

        host, port = server.servers[0].sockets[0].getsockname()[:2]
        try:
            yield f"http://{host}:{port}", store
        finally:
            server.should_exit = True
            thread.join(timeout=5)


def _fetch_login_form(base_url: str) -> tuple[str, dict[str, str]]:
    with httpx.Client(base_url=base_url, follow_redirects=False) as client:
        response = client.get("/admin/login")
        assert response.status_code == 200
        match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
        assert match is not None
        token = match.group(1)
        cookies = {cookie.name: cookie.value for cookie in response.cookies.jar}
        return token, cookies


@pytest.mark.integration
def test_uvicorn_login_limiter_ignores_spoofed_leftmost_xff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _uvicorn_server(monkeypatch) as (base_url, store):
        csrf_token, cookies = _fetch_login_form(base_url)
        data = {
            "username": "ghost",
            "password": "wrong",
        }
        with httpx.Client(base_url=base_url, follow_redirects=False) as client:
            headers = {"X-Forwarded-For": f"{SPOOFED_CLIENT}, {REAL_CLIENT}"}
            first = client.post(
                "/admin/login",
                data={**data, "csrf_token": csrf_token},
                cookies=cookies,
                headers=headers,
            )
            assert first.status_code == 401
            match = re.search(r'name="csrf_token" value="([^"]+)"', first.text)
            assert match is not None
            second = client.post(
                "/admin/login",
                data={**data, "csrf_token": match.group(1)},
                cookies={cookie.name: cookie.value for cookie in first.cookies.jar},
                headers=headers,
            )
            assert second.status_code == 401
            match = re.search(r'name="csrf_token" value="([^"]+)"', second.text)
            assert match is not None
            rotated = {"X-Forwarded-For": f"203.0.113.12, {REAL_CLIENT}"}
            third = client.post(
                "/admin/login",
                data={**data, "csrf_token": match.group(1)},
                cookies={cookie.name: cookie.value for cookie in second.cookies.jar},
                headers=rotated,
            )
            assert third.status_code == 429

    source_key = admin_auth.build_source_rate_limit_key(REAL_CLIENT)
    assert len(store) == 1
    assert source_key in store


@pytest.mark.integration
def test_health_reports_proxy_trust_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", RENDER_TRUSTED_CIDRS)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)
    payload = client.get("/health").json()
    trust = payload["admin_proxy_trust"]
    assert trust["trust_proxy_headers"] is True
    assert trust["trusted_proxy_cidrs_configured"] is True
    assert trust["uvicorn_proxy_headers_disabled"] is True
