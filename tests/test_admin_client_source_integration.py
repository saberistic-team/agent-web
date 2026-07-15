"""Integration tests for admin login client source + limiter behavior (#239)."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from argon2 import PasswordHasher
from fastapi import Request
from uvicorn.middleware.proxy_headers import _TrustedHosts

from app import admin_auth
from app.admin_client_source import resolve_admin_login_client_source, reset_client_source_telemetry_for_tests
from app.config import get_settings

from tests.test_admin_auth import (
    FakeRateLimitStore,
    TEST_PASSWORD,
    TEST_USERNAME,
    _login,
    shared_rate_limiter,
)

pytest_plugins = ["tests.test_admin_auth"]

RENDER_TRUSTED_PROXIES = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,100.64.0.0/10"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)


def _trusted_proxy_request(
    *,
    peer: str = "10.0.0.2",
    headers: dict[str, str] | None = None,
) -> Request:
    scope: dict[str, Any] = {
        "type": "http",
        "headers": [
            (key.lower().encode("latin-1"), value.encode("latin-1"))
            for key, value in (headers or {}).items()
        ],
        "client": (peer, 12345),
        "method": "POST",
        "path": "/admin/login",
    }
    return Request(scope)


@pytest.fixture(autouse=True)
def integration_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "test-session-secret-32chars-minimum")
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "3")
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_IPS", raising=False)
    monkeypatch.delenv("ADMIN_TRUSTED_EDGE_IPS", raising=False)
    admin_auth.reset_login_rate_limiter()
    reset_client_source_telemetry_for_tests()


@pytest.mark.unit
@pytest.mark.integration
def test_rotating_spoofed_headers_do_not_create_new_source_buckets(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", RENDER_TRUSTED_PROXIES)
    settings = get_settings()
    with shared_rate_limiter(rate_limit_store):
        for index in range(5):
            request = _trusted_proxy_request(
                headers={"X-Forwarded-For": f"203.0.113.{index}, 198.51.100.10"},
            )
            admission = admin_auth.try_admit_login_attempt(
                request,
                settings,
                username=f"probe-{index}",
            )
            if index < 3:
                assert admission.admitted is True
            else:
                assert admission.throttled is True

    source_key = admin_auth.build_source_rate_limit_key("198.51.100.10")
    assert len(rate_limit_store.rows) == 1
    assert source_key in rate_limit_store.rows


@pytest.mark.unit
@pytest.mark.integration
def test_telemetry_and_limiter_state_contain_no_raw_forwarding_data(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", RENDER_TRUSTED_PROXIES)
    settings = get_settings()
    caplog.set_level(logging.INFO)
    request = _trusted_proxy_request(
        headers={"X-Forwarded-For": "203.0.113.99, 198.51.100.10"},
    )
    with shared_rate_limiter(rate_limit_store):
        admin_auth.try_admit_login_attempt(request, settings, username="ghost")

    for record in caplog.records:
        message = record.getMessage()
        assert "203.0.113.99" not in message
        assert "198.51.100.10" not in message
        assert "x-forwarded-for" not in message.lower()

    info_records = [record for record in caplog.records if record.levelno == logging.INFO]
    assert any(record.__dict__.get("client_source_path") for record in info_records)

    for row in rate_limit_store.rows.values():
        assert "203.0.113" not in str(row)
        assert "198.51.100" not in str(row)


@pytest.mark.unit
@pytest.mark.integration
def test_http_login_rotating_spoofed_headers_share_one_source_bucket(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", RENDER_TRUSTED_PROXIES)
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    with shared_rate_limiter(rate_limit_store):
        for index in range(4):
            response = _login(
                username=f"probe-{index}",
                password="wrong",
                headers={"X-Forwarded-For": f"203.0.113.{index}, 198.51.100.10"},
            )
            if index < 2:
                assert response.status_code == 401
            else:
                assert response.status_code == 429

    assert len(rate_limit_store.rows) == 1


@pytest.mark.unit
@pytest.mark.integration
def test_asgi_deployment_keeps_socket_peer_and_app_parses_forwarding_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production uses --no-proxy-headers; the app parser must read X-Forwarded-For."""
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", RENDER_TRUSTED_PROXIES)
    settings = get_settings()
    scope: dict[str, Any] = {
        "type": "http",
        "headers": [(b"x-forwarded-for", b"203.0.113.99, 198.51.100.10")],
        "client": ("10.0.0.2", 50000),
        "method": "POST",
        "path": "/admin/login",
    }
    request = Request(scope)
    resolution = resolve_admin_login_client_source(request, settings)
    assert scope["client"][0] == "10.0.0.2"
    assert resolution.source == "198.51.100.10"
    assert resolution.path == "xff_trusted_chain"


@pytest.mark.unit
@pytest.mark.integration
def test_uvicorn_trusted_host_parser_matches_application_algorithm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guardrail: app right-to-left stripping matches Uvicorn's trusted-client algorithm."""
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", RENDER_TRUSTED_PROXIES)
    settings = get_settings()
    header = "203.0.113.99, 198.51.100.10, 10.0.0.2"
    trusted_hosts = _TrustedHosts(RENDER_TRUSTED_PROXIES)
    uvicorn_host, _port = trusted_hosts.get_trusted_client_address(header)
    request = _trusted_proxy_request(
        peer="10.0.0.2",
        headers={"X-Forwarded-For": header},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert uvicorn_host == resolution.source == "198.51.100.10"
