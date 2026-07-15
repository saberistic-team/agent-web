"""Integration tests for admin login source resolution through ASGI proxy middleware."""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app import admin_auth
from app.config import get_settings
from app.main import app
from app.trusted_proxy_defaults import UVICORN_FORWARDED_ALLOW_IPS
from tests.test_admin_auth import FakeRateLimitStore, shared_rate_limiter

RENDER_PROXY = "10.0.0.5"
CLOUDFLARE_EDGE = "104.16.0.1"
CLIENT_IPV4 = "203.0.113.77"
UNTRUSTED_PEER = "198.51.100.10"
TEST_TRUSTED_CIDRS = "10.0.0.0/8,104.16.0.0/13"


@pytest.fixture
def rate_limit_store() -> FakeRateLimitStore:
    return FakeRateLimitStore()


@pytest.fixture(autouse=True)
def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from argon2 import PasswordHasher

    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", "operator")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", PasswordHasher().hash("wrong-password"))
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "test-session-secret-32chars-minimum")
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", TEST_TRUSTED_CIDRS)
    monkeypatch.setenv("ADMIN_CLOUDFLARE_PROXY_CIDRS", "104.16.0.0/13")
    admin_auth.reset_login_rate_limiter()


@pytest.mark.integration
def test_production_proxy_headers_middleware_uses_render_trusted_hosts() -> None:
    middleware = ProxyHeadersMiddleware(app, trusted_hosts=UVICORN_FORWARDED_ALLOW_IPS)
    trusted = middleware.trusted_hosts
    assert any("10.0.0.0/8" in str(network) for network in trusted.trusted_networks)
    assert trusted.get_trusted_client_address("203.0.113.1, 10.0.0.5")[0] == "203.0.113.1"


@pytest.mark.integration
def test_login_route_throttles_by_trusted_chain_client(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_admin_auth import _fetch_login_form, mock_db_connection

    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")

    def _resolve(_request: Any, _settings: Any) -> str:
        return CLIENT_IPV4

    with (
        shared_rate_limiter(rate_limit_store),
        mock_db_connection(),
        patch(
            "app.admin_routes.admin_auth.resolve_admin_login_client_source_for_limiter",
            side_effect=_resolve,
        ),
    ):
        route_client = TestClient(app, follow_redirects=False)
        for _ in range(2):
            csrf_token, cookies = _fetch_login_form()
            response = route_client.post(
                "/admin/login",
                data={"username": "ghost", "password": "wrong", "csrf_token": csrf_token},
                cookies=cookies,
            )
            assert response.status_code == 401

        csrf_token, cookies = _fetch_login_form()
        blocked = route_client.post(
            "/admin/login",
            data={"username": "ghost", "password": "wrong", "csrf_token": csrf_token},
            cookies=cookies,
        )
        assert blocked.status_code == 429

    source_key = admin_auth.build_source_rate_limit_key(CLIENT_IPV4)
    assert len(rate_limit_store.rows) == 1
    assert source_key in rate_limit_store.rows


@pytest.mark.integration
def test_integration_trusted_peer_required_for_forwarded_identity(
    rate_limit_store: FakeRateLimitStore,
) -> None:
    settings = get_settings()

    with shared_rate_limiter(rate_limit_store):
        for _ in range(3):
            request = MagicMock()
            request.client = MagicMock(host=UNTRUSTED_PEER)
            request.headers = MagicMock()
            request.headers.get = lambda key, default="": {
                "x-forwarded-for": f"{CLIENT_IPV4}, {CLOUDFLARE_EDGE}",
            }.get(key.lower(), default)
            admin_auth.try_admit_login_attempt(request, settings)

    peer_key = admin_auth.build_source_rate_limit_key(UNTRUSTED_PEER)
    client_key = admin_auth.build_source_rate_limit_key(CLIENT_IPV4)
    assert client_key not in rate_limit_store.rows
    assert peer_key in rate_limit_store.rows


@pytest.mark.integration
def test_concurrent_spoof_rotation_still_one_source_bucket(
    rate_limit_store: FakeRateLimitStore,
) -> None:
    settings = get_settings()
    barrier = threading.Barrier(6)
    admitted = {"count": 0}
    lock = threading.Lock()
    now = datetime.now(timezone.utc)
    source_key = admin_auth.build_source_rate_limit_key(CLIENT_IPV4)

    def worker(index: int) -> None:
        barrier.wait()
        spoofed = f"203.0.113.{index}"
        request = MagicMock()
        request.client = MagicMock(host=RENDER_PROXY)
        request.headers = MagicMock()
        request.headers.get = lambda key, default="": {
            "x-forwarded-for": f"{spoofed}, {CLIENT_IPV4}, {CLOUDFLARE_EDGE}",
        }.get(key.lower(), default)
        resolution = admin_auth.resolve_admin_login_client_source_for_limiter(
            request,
            settings,
        )
        assert resolution == CLIENT_IPV4
        admission = rate_limit_store.try_admit(
            (source_key,),
            now,
            rate_limit=5,
            window_seconds=900,
            lockout_seconds=900,
        )
        if admission.admitted:
            with lock:
                admitted["count"] += 1

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert admitted["count"] == 5
    assert len(rate_limit_store.rows) == 1
