"""Tests for trusted-proxy admin login client source resolution (#239)."""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path
from typing import Generator
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi import Request
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app import admin_auth
from app.config import Settings, get_settings
from app.main import app
from app.proxy_trust import (
    SourceResolutionPath,
    normalize_client_address,
    production_forwarded_allow_ips,
    reset_proxy_trust_telemetry,
    resolve_admin_login_client_source,
)
from tests.test_admin_auth import FakeRateLimitStore, rate_limit_store, shared_rate_limiter

REPO_ROOT = Path(__file__).resolve().parent.parent
RENDER_INTERNAL_PEER = "10.0.0.55"
CLOUDFLARE_EDGE = "104.16.0.42"
REAL_CLIENT = "203.0.113.77"
OTHER_CLIENT = "203.0.113.88"
SPOOFED_CLIENT = "203.0.113.99"
DIRECT_PEER = "198.51.100.10"

TEST_TRUSTED_PROXY_IPS = "10.0.0.0/24,127.0.0.1/32,104.16.0.0/13"
PROXY_MIDDLEWARE_TRUST = "127.0.0.1,10.0.0.0/24,104.16.0.0/13"


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


def _settings_with_trust(
    monkeypatch: pytest.MonkeyPatch,
    *,
    trusted_ips: str = TEST_TRUSTED_PROXY_IPS,
) -> Settings:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", trusted_ips)
    return get_settings()


@pytest.fixture(autouse=True)
def admin_proxy_trust_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.test_admin_auth import TEST_HASH, TEST_SECRET, TEST_USERNAME

    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "5")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_WINDOW_SECONDS", "900")
    monkeypatch.setenv("ADMIN_LOGIN_LOCKOUT_SECONDS", "900")
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_IPS", raising=False)
    admin_auth.reset_login_rate_limiter()
    reset_proxy_trust_telemetry()


@pytest.mark.unit
def test_normalize_client_address_formats() -> None:
    assert normalize_client_address("203.0.113.1") == "203.0.113.1"
    assert normalize_client_address(" 2001:db8::1 ") == "2001:db8::1"
    assert normalize_client_address("203.0.113.1:443") == "203.0.113.1"
    assert normalize_client_address("[2001:db8::1]:443") == "2001:db8::1"
    assert normalize_client_address("::ffff:203.0.113.5") == "203.0.113.5"
    assert normalize_client_address("") is None
    assert normalize_client_address("not-an-ip") is None
    assert normalize_client_address("a" * 3000) is None


@pytest.mark.unit
def test_direct_spoof_single_and_multi_value_xff_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_trust(monkeypatch)
    headers = [(b"x-forwarded-for", b"203.0.113.99")]
    request = _request_with_client(DIRECT_PEER, headers=headers)
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.address == DIRECT_PEER
    assert resolution.path is SourceResolutionPath.PEER_DIRECT

    multi_headers = [(b"x-forwarded-for", b"203.0.113.1, 203.0.113.2, 203.0.113.3")]
    request_multi = _request_with_client(DIRECT_PEER, headers=multi_headers)
    resolution_multi = resolve_admin_login_client_source(request_multi, settings)
    assert resolution_multi.address == DIRECT_PEER
    assert resolution_multi.path is SourceResolutionPath.PEER_DIRECT


@pytest.mark.unit
def test_cloudflare_append_ignores_attacker_leftmost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_trust(monkeypatch)
    xff = f"{SPOOFED_CLIENT}, {REAL_CLIENT}, {CLOUDFLARE_EDGE}"
    headers = [(b"x-forwarded-for", xff.encode())]
    request = _request_with_client(RENDER_INTERNAL_PEER, headers=headers)
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.address == REAL_CLIENT
    assert resolution.path is SourceResolutionPath.X_FORWARDED_FOR


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_trust(monkeypatch)
    xff = f"{REAL_CLIENT}, {CLOUDFLARE_EDGE}, {RENDER_INTERNAL_PEER}"
    headers = [(b"x-forwarded-for", xff.encode())]
    request = _request_with_client(RENDER_INTERNAL_PEER, headers=headers)
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.address == REAL_CLIENT
    assert resolution.path is SourceResolutionPath.X_FORWARDED_FOR


@pytest.mark.unit
def test_partial_trust_untrusted_intermediary_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_trust(monkeypatch, trusted_ips="10.0.0.0/24")
    xff = f"{REAL_CLIENT}, {DIRECT_PEER}, {RENDER_INTERNAL_PEER}"
    headers = [(b"x-forwarded-for", xff.encode())]
    request = _request_with_client(RENDER_INTERNAL_PEER, headers=headers)
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.address == DIRECT_PEER
    assert resolution.path is SourceResolutionPath.X_FORWARDED_FOR


@pytest.mark.unit
def test_direct_render_origin_ignores_spoofed_cf_connecting_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_trust(monkeypatch)
    headers = [
        (b"cf-connecting-ip", b"203.0.113.55"),
        (b"x-forwarded-for", b"203.0.113.55"),
    ]
    request = _request_with_client(DIRECT_PEER, headers=headers)
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.address == DIRECT_PEER
    assert resolution.path is SourceResolutionPath.PEER_DIRECT


@pytest.mark.unit
def test_cf_connecting_ip_used_when_edge_proven(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_trust(monkeypatch)
    headers = [
        (b"cf-connecting-ip", REAL_CLIENT.encode()),
        (b"x-forwarded-for", CLOUDFLARE_EDGE.encode()),
    ]
    request = _request_with_client(RENDER_INTERNAL_PEER, headers=headers)
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.address == REAL_CLIENT
    assert resolution.path is SourceResolutionPath.CF_CONNECTING_IP


@pytest.mark.unit
def test_header_precedence_xff_before_cf_and_forwarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_trust(monkeypatch)
    xff = f"{REAL_CLIENT}, {CLOUDFLARE_EDGE}"
    headers = [
        (b"x-forwarded-for", xff.encode()),
        (b"cf-connecting-ip", OTHER_CLIENT.encode()),
        (b"forwarded", f'for="{OTHER_CLIENT}"'.encode()),
    ]
    request = _request_with_client(RENDER_INTERNAL_PEER, headers=headers)
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.address == REAL_CLIENT
    assert resolution.path is SourceResolutionPath.X_FORWARDED_FOR


@pytest.mark.unit
def test_forwarded_header_used_when_xff_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_trust(monkeypatch)
    headers = [(b"forwarded", f'for="{REAL_CLIENT}"'.encode())]
    request = _request_with_client(RENDER_INTERNAL_PEER, headers=headers)
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.address == REAL_CLIENT
    assert resolution.path is SourceResolutionPath.FORWARDED


@pytest.mark.unit
def test_malformed_and_overlong_forwarding_data_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_trust(monkeypatch)
    overlong = ", ".join([f"10.0.0.{i}" for i in range(40)])
    headers = [(b"x-forwarded-for", overlong.encode())]
    request = _request_with_client(RENDER_INTERNAL_PEER, headers=headers)
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.address == RENDER_INTERNAL_PEER
    assert resolution.path is SourceResolutionPath.PEER_FALLBACK

    invalid_headers = [(b"x-forwarded-for", b"not-an-ip, 10.0.0.1")]
    invalid_request = _request_with_client(RENDER_INTERNAL_PEER, headers=invalid_headers)
    invalid_resolution = resolve_admin_login_client_source(invalid_request, settings)
    assert invalid_resolution.path is SourceResolutionPath.PEER_FALLBACK


@pytest.mark.unit
def test_proxy_trust_disabled_ignores_all_forwarding_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    settings = get_settings()
    headers = [
        (b"x-forwarded-for", b"203.0.113.1, 10.0.0.1"),
        (b"cf-connecting-ip", b"203.0.113.2"),
        (b"forwarded", b'for="203.0.113.3"'),
    ]
    request = _request_with_client(RENDER_INTERNAL_PEER, headers=headers)
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.address == RENDER_INTERNAL_PEER
    assert resolution.path is SourceResolutionPath.PEER_DIRECT


@pytest.mark.unit
@pytest.mark.integration
def test_rotating_spoofed_headers_share_one_limiter_bucket(
    monkeypatch: pytest.MonkeyPatch,
    rate_limit_store: FakeRateLimitStore,
) -> None:
    from tests.test_admin_auth import _login

    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    with shared_rate_limiter(rate_limit_store):
        for index in range(5):
            headers = {"X-Forwarded-For": f"203.0.113.{index}"}
            response = _login(username="ghost", password="wrong", headers=headers)
            if index < 2:
                assert response.status_code == 401
            else:
                assert response.status_code == 429

    source_key = admin_auth.build_source_rate_limit_key("testclient")
    assert len(rate_limit_store.rows) == 1
    assert source_key in rate_limit_store.rows


@pytest.mark.unit
def test_telemetry_excludes_raw_addresses(
    caplog: pytest.LogCaptureFixture,
    rate_limit_store: FakeRateLimitStore,
) -> None:
    from unittest.mock import MagicMock

    caplog.set_level(logging.INFO)
    request = _request_with_client(
        DIRECT_PEER,
        headers=[(b"x-forwarded-for", b"203.0.113.99")],
    )
    settings = Settings(
        database_url="postgresql://test:test@localhost:5432/test",
        stripe_secret_key="",
        stripe_webhook_secret="",
        stripe_publishable_key="",
        resend_api_key="",
        from_email="",
        notify_email="",
        base_url="http://testserver",
        plausible_domain="",
        plausible_api_key="",
        analytics_environment="test",
        admin_username="operator",
        admin_password_hash="hash",
        admin_session_secret="secret",
        admin_trust_proxy_headers=True,
        admin_trusted_proxy_ips=TEST_TRUSTED_PROXY_IPS,
    )
    with shared_rate_limiter(rate_limit_store):
        with patch("app.admin_auth.db.db_connection") as db_conn:
            db_conn.return_value.__enter__.return_value = MagicMock()
            db_conn.return_value.__exit__.return_value = None
            admin_auth.try_admit_login_attempt(request, settings, username="ghost")

    combined = caplog.text + str(caplog.records)
    assert "203.0.113.99" not in combined
    assert DIRECT_PEER not in combined
    assert "x-forwarded-for" not in combined.lower()
    assert any(
        record.__dict__.get("source_resolution_path") == "peer_direct"
        for record in caplog.records
    )


@pytest.mark.unit
def test_render_yaml_proxy_trust_configuration_is_consistent() -> None:
    render_yaml = (REPO_ROOT / "render.yaml").read_text(encoding="utf-8")
    lines = render_yaml.splitlines()
    assert "ADMIN_TRUST_PROXY_HEADERS" in render_yaml
    assert 'value: "true"' in render_yaml
    assert "ADMIN_TRUSTED_PROXY_IPS" in render_yaml
    assert "--proxy-headers" in render_yaml
    assert "--forwarded-allow-ips=" in render_yaml
    assert '"*"' not in render_yaml
    assert "forwarded-allow-ips=*" not in render_yaml

    trusted_value = ""
    for index, line in enumerate(lines):
        if "ADMIN_TRUSTED_PROXY_IPS" in line and index + 1 < len(lines):
            trusted_value = lines[index + 1]
            break
    start_cmd = next(
        line for line in render_yaml.splitlines() if "startCommand:" in line
    )
    assert "10.0.0.0/8" in trusted_value
    assert "10.0.0.0/8" in start_cmd
    assert "104.16.0.0/13" in trusted_value
    assert "104.16.0.0/13" in start_cmd


@pytest.mark.unit
def test_production_forwarded_allow_ips_matches_render_start_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_IPS", raising=False)
    settings = get_settings()
    allow_ips = production_forwarded_allow_ips(settings)
    render_yaml = (REPO_ROOT / "render.yaml").read_text(encoding="utf-8")
    start_cmd = next(
        line for line in render_yaml.splitlines() if "startCommand:" in line
    )
    assert allow_ips in start_cmd


@pytest.fixture
def proxy_wrapped_app() -> ProxyHeadersMiddleware:
    return ProxyHeadersMiddleware(app, trusted_hosts=PROXY_MIDDLEWARE_TRUST)


@pytest.mark.integration
def test_uvicorn_proxy_middleware_resolves_client_for_limiter(
    proxy_wrapped_app: ProxyHeadersMiddleware,
    monkeypatch: pytest.MonkeyPatch,
    rate_limit_store: FakeRateLimitStore,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    _settings_with_trust(monkeypatch)

    transport = httpx.ASGITransport(
        app=proxy_wrapped_app,
        client=("127.0.0.1", 12345),
    )

    def _admit(client_ip: str) -> bool:
        scope = {
            "type": "http",
            "headers": [
                (
                    b"x-forwarded-for",
                    f"{client_ip}, {CLOUDFLARE_EDGE}, {RENDER_INTERNAL_PEER}".encode(),
                )
            ],
            "client": ("127.0.0.1", 12345),
            "method": "POST",
            "path": "/admin/login",
            "query_string": b"",
            "root_path": "",
            "scheme": "http",
            "server": ("testserver", 80),
            "http_version": "1.1",
        }
        request = Request(scope)
        with shared_rate_limiter(rate_limit_store):
            return admin_auth.try_admit_login_attempt(
                request,
                get_settings(),
                username="ghost",
            ).admitted

    assert _admit(REAL_CLIENT) is True
    assert _admit(REAL_CLIENT) is True
    assert _admit(REAL_CLIENT) is False
    assert _admit(OTHER_CLIENT) is True


@pytest.mark.integration
def test_uvicorn_proxy_middleware_ignores_spoofed_leftmost(
    proxy_wrapped_app: ProxyHeadersMiddleware,
    monkeypatch: pytest.MonkeyPatch,
    rate_limit_store: FakeRateLimitStore,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "5")
    _settings_with_trust(monkeypatch)

    for index in range(3):
        scope = {
            "type": "http",
            "headers": [
                (
                    b"x-forwarded-for",
                    (
                        f"203.0.113.{index}, {REAL_CLIENT}, {CLOUDFLARE_EDGE}, "
                        f"{RENDER_INTERNAL_PEER}"
                    ).encode(),
                )
            ],
            "client": ("127.0.0.1", 12345),
            "method": "POST",
            "path": "/admin/login",
            "query_string": b"",
            "root_path": "",
            "scheme": "http",
            "server": ("testserver", 80),
            "http_version": "1.1",
        }
        with shared_rate_limiter(rate_limit_store):
            admin_auth.try_admit_login_attempt(
                Request(scope),
                get_settings(),
                username="ghost",
            )

    source_key = admin_auth.build_source_rate_limit_key(REAL_CLIENT)
    assert source_key in rate_limit_store.rows
    assert len(rate_limit_store.rows) == 1


@pytest.mark.integration
def test_subprocess_uvicorn_start_command_includes_proxy_flags() -> None:
    """Smoke-check the documented production start flags parse under uvicorn."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "--help",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "--proxy-headers" in result.stdout
    assert "--forwarded-allow-ips" in result.stdout
