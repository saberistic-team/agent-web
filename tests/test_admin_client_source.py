"""Tests for trusted-proxy admin login client source resolution (#239)."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import httpx
import pytest
from argon2 import PasswordHasher
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app import admin_auth
from app.client_source import (
    PATH_CF_CONNECTING_IP,
    PATH_DIRECT_PEER,
    PATH_MALFORMED_FORWARDED,
    PATH_RFC_FORWARDED,
    PATH_TRUSTED_CHAIN,
    PATH_TRUSTED_PEER_FALLBACK,
    PATH_UNTRUSTED_FORWARDED,
    TrustedProxyBoundary,
    reset_untrusted_forwarded_telemetry,
    resolve_admin_login_client_source,
)
from app.config import get_settings

REPO_ROOT = Path(__file__).resolve().parent.parent

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "test-session-secret-32chars-minimum"


def _request_with_client(
    host: str,
    *,
    headers: dict[str, str] | None = None,
) -> Request:
    header_list = [
        (key.lower().encode("latin1"), value.encode("latin1"))
        for key, value in (headers or {}).items()
    ]
    scope = {
        "type": "http",
        "headers": header_list,
        "client": (host, 12345),
        "method": "POST",
        "path": "/admin/login",
    }
    return Request(scope)


@pytest.fixture(autouse=True)
def _admin_client_source_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "5")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_WINDOW_SECONDS", "900")
    monkeypatch.setenv("ADMIN_LOGIN_LOCKOUT_SECONDS", "900")
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    monkeypatch.delenv("FORWARDED_ALLOW_IPS", raising=False)
    admin_auth.reset_login_rate_limiter()
    reset_untrusted_forwarded_telemetry()


@pytest.mark.unit
def test_direct_spoof_single_and_multi_hop_xff_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    settings = get_settings()
    for header_value in ("203.0.113.99", "203.0.113.1, 203.0.113.2, 198.51.100.10"):
        request = _request_with_client(
            "198.51.100.10",
            headers={"X-Forwarded-For": header_value},
        )
        resolution = resolve_admin_login_client_source(request, settings)
        assert resolution.source == "198.51.100.10"
        assert resolution.path == PATH_DIRECT_PEER


@pytest.mark.unit
def test_cloudflare_append_selects_connecting_address_not_leftmost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "10.0.0.1")
    settings = get_settings()
    request = _request_with_client(
        "10.0.0.1",
        headers={"X-Forwarded-For": "203.0.113.1, 198.51.100.10, 10.0.0.1"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "198.51.100.10"
    assert resolution.path == PATH_TRUSTED_CHAIN


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "10.0.0.1,172.16.0.0/16")
    settings = get_settings()
    request = _request_with_client(
        "10.0.0.1",
        headers={"X-Forwarded-For": "203.0.113.50, 172.16.1.2, 10.0.0.1"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.50"
    assert resolution.path == PATH_TRUSTED_CHAIN


@pytest.mark.unit
def test_partial_trust_untrusted_intermediary_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "10.0.0.1")
    settings = get_settings()
    request = _request_with_client(
        "198.51.100.10",
        headers={"X-Forwarded-For": "203.0.113.1, 10.0.0.1"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "unknown"
    assert resolution.path == PATH_UNTRUSTED_FORWARDED


@pytest.mark.unit
def test_direct_render_origin_ignores_cf_connecting_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "10.0.0.1")
    settings = get_settings()
    request = _request_with_client(
        "10.0.0.1",
        headers={"CF-Connecting-IP": "203.0.113.77"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "unknown"
    assert resolution.path == PATH_UNTRUSTED_FORWARDED


@pytest.mark.unit
def test_cf_connecting_ip_used_when_multi_hop_chain_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "10.0.0.1")
    settings = get_settings()
    request = _request_with_client(
        "10.0.0.1",
        headers={
            "X-Forwarded-For": "203.0.113.1, 203.0.113.77, 10.0.0.1",
            "CF-Connecting-IP": "203.0.113.77",
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.77"
    assert resolution.path == PATH_CF_CONNECTING_IP


@pytest.mark.unit
def test_header_precedence_xff_wins_over_conflicting_forwarded_and_cf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "10.0.0.1")
    settings = get_settings()
    request = _request_with_client(
        "10.0.0.1",
        headers={
            "X-Forwarded-For": "203.0.113.10, 203.0.113.20, 10.0.0.1",
            "Forwarded": 'for=203.0.113.99;proto=https',
            "CF-Connecting-IP": "203.0.113.55",
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.20"
    assert resolution.path == PATH_TRUSTED_CHAIN


@pytest.mark.unit
def test_rfc_forwarded_used_when_xff_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "10.0.0.1")
    settings = get_settings()
    request = _request_with_client(
        "10.0.0.1",
        headers={"Forwarded": 'for="[2001:db8::1]:4711";proto=https'},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "2001:db8::1"
    assert resolution.path == PATH_RFC_FORWARDED


@pytest.mark.unit
@pytest.mark.parametrize(
    ("header_value", "expected"),
    [
        ("203.0.113.1", "203.0.113.1"),
        ("2001:db8::1", "2001:db8::1"),
        ("203.0.113.1:12345", "203.0.113.1"),
        ("::ffff:203.0.113.1", "203.0.113.1"),
        ("  203.0.113.1  ", "203.0.113.1"),
    ],
)
def test_address_formats_normalize_deterministically(
    monkeypatch: pytest.MonkeyPatch,
    header_value: str,
    expected: str,
) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "10.0.0.1")
    settings = get_settings()
    request = _request_with_client(
        "10.0.0.1",
        headers={"X-Forwarded-For": f"{header_value}, 10.0.0.1"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == expected


@pytest.mark.unit
def test_malformed_and_overlong_chains_fall_back_to_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "10.0.0.1")
    settings = get_settings()

    invalid = _request_with_client(
        "10.0.0.1",
        headers={"X-Forwarded-For": "not-an-ip, 10.0.0.1"},
    )
    invalid_resolution = resolve_admin_login_client_source(invalid, settings)
    assert invalid_resolution.source == "unknown"
    assert invalid_resolution.path == PATH_MALFORMED_FORWARDED

    overlong = ", ".join([f"203.0.113.{i % 250}" for i in range(40)] + ["10.0.0.1"])
    overlong_request = _request_with_client(
        "10.0.0.1",
        headers={"X-Forwarded-For": overlong},
    )
    overlong_resolution = resolve_admin_login_client_source(overlong_request, settings)
    assert overlong_resolution.source == "unknown"
    assert overlong_resolution.path == PATH_MALFORMED_FORWARDED


@pytest.mark.unit
def test_single_hop_trusted_peer_uses_peer_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "10.0.0.1")
    settings = get_settings()
    request = _request_with_client(
        "10.0.0.1",
        headers={"X-Forwarded-For": "203.0.113.50"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "unknown"
    assert resolution.path == PATH_TRUSTED_PEER_FALLBACK


@pytest.mark.unit
def test_trusted_proxy_boundary_matches_uvicorn_algorithm() -> None:
    boundary = TrustedProxyBoundary("10.0.0.1")
    assert boundary.client_from_x_forwarded_for("203.0.113.1, 10.0.0.1") == "203.0.113.1"
    assert boundary.client_from_x_forwarded_for("203.0.113.1") == "203.0.113.1"


@pytest.mark.unit
@pytest.mark.integration
def test_rotating_spoofed_headers_do_not_create_new_limiter_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_admin_auth import FakeRateLimitStore, _login, mock_db_connection, shared_rate_limiter

    store = FakeRateLimitStore()
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "3")
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "testclient")
    with shared_rate_limiter(store):
        with mock_db_connection():
            for index in range(5):
                response = _login(
                    username=f"user-{index}",
                    password="wrong",
                    headers={
                        "X-Forwarded-For": f"203.0.113.{index}, 198.51.100.10, testclient",
                    },
                )
                if index < 3:
                    assert response.status_code == 401
                else:
                    assert response.status_code == 429

    source_key = admin_auth.build_source_rate_limit_key("198.51.100.10")
    assert len(store.rows) == 1
    assert source_key in store.rows


@pytest.mark.unit
def test_render_yaml_proxy_trust_configuration_is_consistent() -> None:
    render_yaml = (REPO_ROOT / "render.yaml").read_text(encoding="utf-8")
    assert "--proxy-headers" in render_yaml
    assert "--forwarded-allow-ips" in render_yaml
    assert "FORWARDED_ALLOW_IPS" in render_yaml
    assert "ADMIN_TRUST_PROXY_HEADERS" in render_yaml
    assert 'value: "true"' in render_yaml or "value: 'true'" in render_yaml

    admin_auth_doc = (REPO_ROOT / "docs" / "ADMIN_AUTH.md").read_text(encoding="utf-8")
    assert "FORWARDED_ALLOW_IPS" in admin_auth_doc
    assert "--forwarded-allow-ips" in admin_auth_doc
    assert "right-to-left" in admin_auth_doc.lower()


@pytest.mark.unit
def test_privacy_no_raw_addresses_in_logs_or_limiter_rows(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from tests.test_admin_auth import FakeRateLimitStore, _login, mock_db_connection, shared_rate_limiter

    store = FakeRateLimitStore()
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "testclient")
    caplog.set_level(logging.INFO)
    with shared_rate_limiter(store):
        with mock_db_connection():
            response = _login(
                password="wrong",
                headers={"X-Forwarded-For": "203.0.113.55, 198.51.100.10, testclient"},
            )
    assert response.status_code == 401

    for record in caplog.records:
        message = record.getMessage()
        assert "203.0.113.55" not in message
        assert "x-forwarded-for" not in message.lower()
        if hasattr(record, "resolution_path"):
            assert "203.0.113" not in str(record.resolution_path)

    for row in store.rows.values():
        assert "203.0.113" not in str(row)


@pytest.mark.unit
def test_health_reports_proxy_trust_mode_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    response = TestClient(app).get("/health")
    assert response.status_code == 200
    assert response.json()["admin_client_source_trust"] == "proxy_boundary"


@pytest.mark.unit
@pytest.mark.integration
def test_uvicorn_proxy_headers_integration_resolves_client_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "10.0.0.1")

    async def _run() -> None:
        async def echo_client(request: Request) -> JSONResponse:
            settings = get_settings()
            resolution = resolve_admin_login_client_source(request, settings)
            return JSONResponse(
                {
                    "source": resolution.source,
                    "path": resolution.path,
                    "peer": request.client.host if request.client else None,
                }
            )

        app = FastAPI()
        app.add_api_route("/source", echo_client, methods=["GET"])
        wrapped = ProxyHeadersMiddleware(app, trusted_hosts="10.0.0.1")

        transport = httpx.ASGITransport(app=wrapped, client=("10.0.0.1", 50000))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            spoofed = await client.get(
                "/source",
                headers={"X-Forwarded-For": "203.0.113.1"},
            )
            assert spoofed.json()["source"] == "unknown"
            assert spoofed.json()["path"] == PATH_TRUSTED_PEER_FALLBACK

            chained = await client.get(
                "/source",
                headers={"X-Forwarded-For": "203.0.113.1, 198.51.100.10, 10.0.0.1"},
            )
            assert chained.json()["source"] == "198.51.100.10"
            assert chained.json()["path"] == PATH_TRUSTED_CHAIN

        untrusted_transport = httpx.ASGITransport(app=wrapped, client=("198.51.100.10", 50000))
        async with httpx.AsyncClient(
            transport=untrusted_transport,
            base_url="http://testserver",
        ) as client:
            direct = await client.get(
                "/source",
                headers={"X-Forwarded-For": "203.0.113.9"},
            )
            assert direct.json()["source"] == "unknown"
            assert direct.json()["path"] == PATH_TRUSTED_PEER_FALLBACK

    asyncio.run(_run())
