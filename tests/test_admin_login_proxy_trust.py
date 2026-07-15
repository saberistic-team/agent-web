"""Trusted-proxy client source resolution for admin login rate limiting (#239)."""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import patch

import pytest
from argon2 import PasswordHasher
from fastapi import Request
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app import admin_auth
from app.admin_auth import LOGIN_FLOW_COOKIE_NAME
from app.config import get_settings
from app.main import app
from app.proxy_trust import (
    PATH_CF_CONNECTING_IP,
    PATH_CONSERVATIVE_AMBIGUOUS,
    PATH_CONSERVATIVE_UNKNOWN,
    PATH_CONSERVATIVE_UNTRUSTED_HEADERS,
    PATH_DIRECT_PEER,
    PATH_FORWARDED_HEADER,
    PATH_XFF_TRUSTED_WALK,
    PRODUCTION_TRUSTED_PROXY_CIDRS,
    normalize_client_address,
    reset_proxy_trust_telemetry,
    resolve_admin_login_client_source,
)
from fastapi.testclient import TestClient

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "test-session-secret-32chars-minimum"
RENDER_PROXY = "10.0.0.1"
RENDER_PROXY_CIDRS = "10.0.0.0/8,172.16.0.0/12,100.64.0.0/10"
REAL_CLIENT = "203.0.113.50"
SPOOFED_CLIENT = "198.51.100.99"


def _request_with_client(
    host: str,
    *,
    headers: list[tuple[bytes, bytes]] | None = None,
) -> Request:
    scope = {
        "type": "http",
        "headers": headers or [],
        "client": (host, 12345),
        "method": "POST",
        "path": "/admin/login",
    }
    return Request(scope)


def _header(name: str, value: str) -> tuple[bytes, bytes]:
    return (name.lower().encode("ascii"), value.encode("ascii"))


@pytest.fixture(autouse=True)
def proxy_trust_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    reset_proxy_trust_telemetry()
    admin_auth.reset_login_rate_limiter()


@pytest.fixture
def trusted_proxy_settings(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", RENDER_PROXY_CIDRS)
    return get_settings()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("203.0.113.1", "203.0.113.1"),
        ("203.0.113.1:443", "203.0.113.1"),
        ("2001:db8::1", "2001:db8::1"),
        ("[2001:db8::1]:443", "2001:db8::1"),
        ("::ffff:203.0.113.1", "203.0.113.1"),
        ("  203.0.113.1  ", "203.0.113.1"),
        ("", None),
        ("not-an-ip", None),
        ("[broken", None),
    ],
)
def test_normalize_client_address(raw: str, expected: str | None) -> None:
    assert normalize_client_address(raw) == expected


@pytest.mark.unit
def test_direct_spoof_single_and_multi_value_xff_ignored(
    trusted_proxy_settings,
) -> None:
    settings = trusted_proxy_settings
    for header_value in (
        SPOOFED_CLIENT,
        f"{SPOOFED_CLIENT}, {REAL_CLIENT}",
        f"{SPOOFED_CLIENT}, {REAL_CLIENT}, {RENDER_PROXY}",
    ):
        request = _request_with_client(
            REAL_CLIENT,
            headers=[_header("x-forwarded-for", header_value)],
        )
        resolution = resolve_admin_login_client_source(request, settings)
        assert resolution.address == REAL_CLIENT
        assert resolution.path == PATH_DIRECT_PEER


@pytest.mark.unit
def test_cloudflare_append_selects_real_client_not_leftmost(
    trusted_proxy_settings,
) -> None:
    settings = trusted_proxy_settings
    request = _request_with_client(
        RENDER_PROXY,
        headers=[
            _header(
                "x-forwarded-for",
                f"{SPOOFED_CLIENT}, {REAL_CLIENT}, {RENDER_PROXY}",
            )
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.address == REAL_CLIENT
    assert resolution.path == PATH_XFF_TRUSTED_WALK


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client(trusted_proxy_settings) -> None:
    settings = trusted_proxy_settings
    request = _request_with_client(
        RENDER_PROXY,
        headers=[_header("x-forwarded-for", f"{REAL_CLIENT}, {RENDER_PROXY}")],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.address == REAL_CLIENT
    assert resolution.path == PATH_XFF_TRUSTED_WALK


@pytest.mark.unit
def test_partial_trust_untrusted_intermediary_fails_closed(
    trusted_proxy_settings,
) -> None:
    settings = trusted_proxy_settings
    untrusted_proxy = "198.51.100.10"
    request = _request_with_client(
        RENDER_PROXY,
        headers=[
            _header(
                "x-forwarded-for",
                f"{SPOOFED_CLIENT}, {REAL_CLIENT}, {untrusted_proxy}, {RENDER_PROXY}",
            )
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.address == untrusted_proxy
    assert resolution.path == PATH_XFF_TRUSTED_WALK


@pytest.mark.unit
def test_direct_render_origin_ignores_cf_connecting_ip(
    trusted_proxy_settings,
) -> None:
    settings = trusted_proxy_settings
    request = _request_with_client(
        REAL_CLIENT,
        headers=[_header("cf-connecting-ip", SPOOFED_CLIENT)],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.address == REAL_CLIENT
    assert resolution.path == PATH_DIRECT_PEER


@pytest.mark.unit
def test_header_precedence_xff_over_forwarded_and_cf(
    trusted_proxy_settings,
) -> None:
    settings = trusted_proxy_settings
    request = _request_with_client(
        RENDER_PROXY,
        headers=[
            _header("x-forwarded-for", f"{REAL_CLIENT}, {RENDER_PROXY}"),
            _header("forwarded", f'for="{SPOOFED_CLIENT}"'),
            _header("cf-connecting-ip", SPOOFED_CLIENT),
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.address == REAL_CLIENT
    assert resolution.path == PATH_XFF_TRUSTED_WALK


@pytest.mark.unit
def test_forwarded_header_used_when_xff_missing(trusted_proxy_settings) -> None:
    settings = trusted_proxy_settings
    request = _request_with_client(
        RENDER_PROXY,
        headers=[_header("forwarded", f'for="{REAL_CLIENT}";proto=https')],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.address == REAL_CLIENT
    assert resolution.path == PATH_FORWARDED_HEADER


@pytest.mark.unit
def test_cf_connecting_ip_used_when_other_headers_missing(
    trusted_proxy_settings,
) -> None:
    settings = trusted_proxy_settings
    request = _request_with_client(
        RENDER_PROXY,
        headers=[_header("cf-connecting-ip", REAL_CLIENT)],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.address == REAL_CLIENT
    assert resolution.path == PATH_CF_CONNECTING_IP


@pytest.mark.unit
def test_overlong_forward_chain_fails_closed(trusted_proxy_settings) -> None:
    settings = trusted_proxy_settings
    chain = ", ".join([f"203.0.113.{i}" for i in range(40)] + [RENDER_PROXY])
    request = _request_with_client(
        RENDER_PROXY,
        headers=[_header("x-forwarded-for", chain)],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.address == "unknown"
    assert resolution.path == PATH_CONSERVATIVE_AMBIGUOUS


@pytest.mark.unit
def test_inconsistent_rightmost_xff_peer_fails_closed_to_peer(
    trusted_proxy_settings,
) -> None:
    settings = trusted_proxy_settings
    request = _request_with_client(
        RENDER_PROXY,
        headers=[_header("x-forwarded-for", f"{REAL_CLIENT}, 10.0.0.2")],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.address == RENDER_PROXY
    assert resolution.path == PATH_CONSERVATIVE_UNTRUSTED_HEADERS


@pytest.mark.unit
def test_empty_xff_elements_and_whitespace_are_ignored_in_chain(
    trusted_proxy_settings,
) -> None:
    settings = trusted_proxy_settings
    request = _request_with_client(
        RENDER_PROXY,
        headers=[_header("x-forwarded-for", f"  {REAL_CLIENT}  , , {RENDER_PROXY}  ")],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.address == REAL_CLIENT
    assert resolution.path == PATH_XFF_TRUSTED_WALK


@pytest.mark.unit
def test_malformed_xff_returns_unknown(trusted_proxy_settings) -> None:
    settings = trusted_proxy_settings
    request = _request_with_client(
        RENDER_PROXY,
        headers=[_header("x-forwarded-for", f"not-an-ip, {RENDER_PROXY}")],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.address == "unknown"
    assert resolution.path == PATH_CONSERVATIVE_UNKNOWN


@pytest.mark.unit
def test_trust_disabled_ignores_headers_even_with_cidrs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", RENDER_PROXY_CIDRS)
    settings = get_settings()
    request = _request_with_client(
        RENDER_PROXY,
        headers=[_header("x-forwarded-for", f"{SPOOFED_CLIENT}, {RENDER_PROXY}")],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.address == RENDER_PROXY
    assert resolution.path == PATH_DIRECT_PEER


@pytest.mark.unit
def test_trust_enabled_without_cidrs_ignores_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    settings = get_settings()
    request = _request_with_client(
        RENDER_PROXY,
        headers=[_header("x-forwarded-for", f"{REAL_CLIENT}, {RENDER_PROXY}")],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.address == RENDER_PROXY
    assert resolution.path == PATH_DIRECT_PEER


@pytest.mark.unit
def test_rotating_spoofed_headers_do_not_create_new_source_buckets(
    trusted_proxy_settings,
) -> None:
    settings = trusted_proxy_settings
    keys: set[str] = set()
    for spoofed in (f"198.51.100.{i}" for i in range(10)):
        request = _request_with_client(
            RENDER_PROXY,
            headers=[
                _header(
                    "x-forwarded-for",
                    f"{spoofed}, {REAL_CLIENT}, {RENDER_PROXY}",
                )
            ],
        )
        source = admin_auth.client_ip(request, settings)
        keys.add(admin_auth.build_source_rate_limit_key(source))

    assert keys == {admin_auth.build_source_rate_limit_key(REAL_CLIENT)}


@pytest.mark.unit
@pytest.mark.integration
def test_uvicorn_proxy_headers_stack_matches_resolver(
    trusted_proxy_settings,
) -> None:
    settings = trusted_proxy_settings
    captured: dict[str, Any] = {}

    async def _capture_app(scope, receive, send):  # noqa: ANN001
        if scope["type"] == "lifespan":
            await send({"type": "lifespan.startup", "state": {}})
            await send({"type": "lifespan.shutdown"})
            return
        request = Request(scope)
        captured["client_host"] = request.client.host if request.client else None
        captured["resolution"] = resolve_admin_login_client_source(request, settings)
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    wrapped = ProxyHeadersMiddleware(_capture_app, trusted_hosts=RENDER_PROXY_CIDRS)
    proxy_client = TestClient(wrapped, client=(RENDER_PROXY, 12345))
    response = proxy_client.post(
        "/admin/login",
        headers={
            "X-Forwarded-For": f"{SPOOFED_CLIENT}, {REAL_CLIENT}, {RENDER_PROXY}",
        },
    )
    assert response.status_code == 204
    assert captured["client_host"] == REAL_CLIENT
    assert captured["resolution"].address == REAL_CLIENT
    assert captured["resolution"].path == PATH_DIRECT_PEER


@pytest.mark.unit
def test_telemetry_logs_resolution_path_without_raw_addresses(
    trusted_proxy_settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = trusted_proxy_settings
    request = _request_with_client(
        REAL_CLIENT,
        headers=[_header("x-forwarded-for", SPOOFED_CLIENT)],
    )
    with caplog.at_level(logging.INFO):
        with (
            patch("app.admin_auth.db.try_admit_admin_login") as admit,
            patch("app.admin_auth.db.cleanup_expired_admin_login_rate_limits"),
            patch("app.admin_auth.db.db_connection") as db_conn,
        ):
            from app import db

            admit.return_value = db.AdminLoginAdmission(
                admitted=True,
                throttled=False,
                already_locked=False,
                lockout_transition=False,
            )
            db_conn.return_value.__enter__.return_value = object()
            db_conn.return_value.__exit__.return_value = None
            admin_auth.try_admit_login_attempt(request, settings, username="ghost")

    logged = " ".join(caplog.messages + [str(record.msg) for record in caplog.records])
    assert SPOOFED_CLIENT not in logged
    assert REAL_CLIENT not in logged
    assert "x-forwarded-for" not in logged.lower()
    assert any(
        record.__dict__.get("source_resolution_path") == PATH_DIRECT_PEER
        for record in caplog.records
    )


@pytest.mark.unit
def test_limiter_rows_store_digests_not_raw_addresses(
    trusted_proxy_settings,
) -> None:
    settings = trusted_proxy_settings
    request = _request_with_client(
        RENDER_PROXY,
        headers=[_header("x-forwarded-for", f"{REAL_CLIENT}, {RENDER_PROXY}")],
    )
    source = admin_auth.client_ip(request, settings)
    key = admin_auth.build_source_rate_limit_key(source)
    assert REAL_CLIENT not in key
    assert len(key) == 64


@pytest.mark.unit
def test_render_yaml_proxy_settings_match_production_constant() -> None:
    from pathlib import Path

    render_yaml = (Path(__file__).resolve().parent.parent / "render.yaml").read_text()
    expected_cidrs = ",".join(PRODUCTION_TRUSTED_PROXY_CIDRS)
    assert "--proxy-headers" in render_yaml
    assert f"--forwarded-allow-ips={expected_cidrs}" in render_yaml
    assert "ADMIN_TRUST_PROXY_HEADERS" in render_yaml
    assert f'value: "{expected_cidrs}"' in render_yaml


@pytest.mark.unit
def test_health_reports_proxy_trust_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", RENDER_PROXY_CIDRS)
    client = TestClient(app)
    payload = client.get("/health").json()
    assert payload["admin_proxy_trust"] == {
        "enabled": True,
        "trusted_cidr_count": len(PRODUCTION_TRUSTED_PROXY_CIDRS),
        "uvicorn_proxy_headers": True,
    }
