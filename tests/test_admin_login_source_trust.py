"""Trusted-proxy client source resolution for admin login (#239)."""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest
from fastapi import Request

from argon2 import PasswordHasher

from app import admin_auth
from app.config import get_settings
from app.proxy_trust import (
    PATH_CF_CONNECTING_IP_VERIFIED,
    PATH_DIRECT_PEER,
    PATH_FORWARDED_RFC7239,
    PATH_MALFORMED_CHAIN,
    PATH_UNTRUSTED_HEADERS_IGNORED,
    PATH_XFF_TRUSTED_WALK,
    normalize_ip_address,
    parse_forwarded_header,
    parse_x_forwarded_for_chain,
    resolve_admin_login_client_source,
)
from tests.test_admin_auth import (
    TEST_PASSWORD,
    TEST_USERNAME,
    FakeRateLimitStore,
    _request_with_client,
    shared_rate_limiter,
)

RENDER_LB = "10.0.0.1"
UNTRUSTED_PEER = "198.51.100.10"
CLIENT_A = "203.0.113.50"
CLIENT_B = "203.0.113.77"
SPOOFED = "203.0.113.99"
CLOUDFLARE_EDGE = "103.21.244.1"

TRUSTED_CIDRS = "10.0.0.0/8,127.0.0.1"
CLOUDFLARE_CIDRS = "103.21.244.0/22"

_TEST_HASH = PasswordHasher().hash("correct-horse-battery-staple")
_TEST_SECRET = "test-session-secret-32chars-minimum"


@pytest.fixture
def rate_limit_store() -> FakeRateLimitStore:
    return FakeRateLimitStore()


@pytest.fixture(autouse=True)
def source_trust_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", _TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", _TEST_SECRET)
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    monkeypatch.delenv("ADMIN_TRUSTED_CLOUDFLARE_CIDRS", raising=False)
    admin_auth.reset_login_rate_limiter()


def _settings_env(monkeypatch: pytest.MonkeyPatch, **overrides: str | None) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", _TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", _TEST_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    for key, value in overrides.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)


def _request(
    *,
    peer: str,
    headers: list[tuple[bytes, bytes]] | None = None,
) -> Request:
    request = _request_with_client(peer)
    if headers:
        request.headers.__dict__["_list"].extend(headers)
    return request


@pytest.fixture
def trusted_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings_env(
        monkeypatch,
        ADMIN_TRUSTED_PROXY_CIDRS=TRUSTED_CIDRS,
        ADMIN_TRUSTED_CLOUDFLARE_CIDRS=CLOUDFLARE_CIDRS,
    )


@pytest.mark.unit
def test_direct_spoof_single_and_multi_value_xff_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings_env(monkeypatch, ADMIN_TRUSTED_PROXY_CIDRS=None)
    settings = get_settings()

    single = _request(
        peer=UNTRUSTED_PEER,
        headers=[(b"x-forwarded-for", b"203.0.113.1")],
    )
    multi = _request(
        peer=UNTRUSTED_PEER,
        headers=[(b"x-forwarded-for", b"203.0.113.1, 203.0.113.2, 10.0.0.1")],
    )

    assert resolve_admin_login_client_source(single, settings).source == UNTRUSTED_PEER
    assert resolve_admin_login_client_source(multi, settings).source == UNTRUSTED_PEER
    assert (
        resolve_admin_login_client_source(single, settings).path
        == PATH_UNTRUSTED_HEADERS_IGNORED
    )


@pytest.mark.unit
def test_cloudflare_append_selects_connecting_client_not_leftmost(
    trusted_env: None,
) -> None:
    settings = get_settings()
    request = _request(
        peer=RENDER_LB,
        headers=[(b"x-forwarded-for", f"{SPOOFED}, {CLIENT_A}".encode())],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == CLIENT_A
    assert resolution.path == PATH_XFF_TRUSTED_WALK


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client(trusted_env: None) -> None:
    settings = get_settings()
    request = _request(
        peer=RENDER_LB,
        headers=[(b"x-forwarded-for", f"{CLIENT_B}, {CLOUDFLARE_EDGE}".encode())],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == CLIENT_B
    assert resolution.path == PATH_XFF_TRUSTED_WALK


@pytest.mark.unit
def test_partial_trust_fails_closed_for_untrusted_intermediary(
    trusted_env: None,
) -> None:
    settings = get_settings()
    request = _request(
        peer=UNTRUSTED_PEER,
        headers=[(b"x-forwarded-for", f"{CLIENT_A}, {RENDER_LB}".encode())],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == UNTRUSTED_PEER
    assert resolution.path == PATH_UNTRUSTED_HEADERS_IGNORED


@pytest.mark.unit
def test_direct_render_origin_ignores_cloudflare_vendor_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings_env(
        monkeypatch,
        ADMIN_TRUSTED_PROXY_CIDRS=TRUSTED_CIDRS,
        ADMIN_TRUSTED_CLOUDFLARE_CIDRS=CLOUDFLARE_CIDRS,
    )
    settings = get_settings()
    request = _request(
        peer=UNTRUSTED_PEER,
        headers=[
            (b"cf-connecting-ip", b"203.0.113.88"),
            (b"x-forwarded-for", b"203.0.113.88"),
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == UNTRUSTED_PEER
    assert resolution.path == PATH_UNTRUSTED_HEADERS_IGNORED


@pytest.mark.unit
def test_header_precedence_prefers_xff_walk_over_cf_connecting_ip(
    trusted_env: None,
) -> None:
    settings = get_settings()
    request = _request(
        peer=RENDER_LB,
        headers=[
            (b"x-forwarded-for", f"{CLIENT_A}, {CLOUDFLARE_EDGE}".encode()),
            (b"cf-connecting-ip", b"203.0.113.88"),
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == CLIENT_A
    assert resolution.path == PATH_XFF_TRUSTED_WALK


@pytest.mark.unit
def test_forwarded_header_used_when_xff_missing(trusted_env: None) -> None:
    settings = get_settings()
    request = _request(
        peer=RENDER_LB,
        headers=[(b"forwarded", f'for="{CLIENT_B}";proto=https'.encode())],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == CLIENT_B
    assert resolution.path == PATH_FORWARDED_RFC7239


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
    ],
)
def test_normalize_ip_address_formats(raw: str, expected: str | None) -> None:
    assert normalize_ip_address(raw) == expected


@pytest.mark.unit
def test_xff_parser_rejects_invalid_and_overlong_chains() -> None:
    assert parse_x_forwarded_for_chain("203.0.113.1, not-an-ip") == []
    assert parse_x_forwarded_for_chain(" , ") == []
    long_chain = ", ".join(["203.0.113.1"] * (32 + 1))
    assert parse_x_forwarded_for_chain(long_chain) == []


@pytest.mark.unit
def test_forwarded_parser_handles_ipv6_and_whitespace() -> None:
    assert parse_forwarded_header(' for="[2001:db8::2]" ;proto=https') == ["2001:db8::2"]
    assert parse_forwarded_header("for=unknown") == []


@pytest.mark.unit
@pytest.mark.integration
def test_rotating_spoofed_headers_do_not_create_new_source_buckets(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings_env(
        monkeypatch,
        ADMIN_TRUSTED_PROXY_CIDRS=TRUSTED_CIDRS,
        ADMIN_LOGIN_RATE_LIMIT="2",
    )
    admin_auth.reset_login_rate_limiter()
    with shared_rate_limiter(rate_limit_store):
        for index in range(4):
            request = _request(
                peer=UNTRUSTED_PEER,
                headers=[(b"x-forwarded-for", f"203.0.113.{index}".encode())],
            )
            admission = admin_auth.try_admit_login_attempt(
                request,
                get_settings(),
                username="ghost",
            )
            if index < 2:
                assert admission.admitted
            else:
                assert admission.throttled

    assert len(rate_limit_store.rows) == 1
    source_key = admin_auth.build_source_rate_limit_key(UNTRUSTED_PEER)
    assert source_key in rate_limit_store.rows


@pytest.mark.unit
@pytest.mark.integration
def test_trusted_proxy_rotating_leftmost_values_share_one_bucket(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings_env(
        monkeypatch,
        ADMIN_TRUSTED_PROXY_CIDRS=TRUSTED_CIDRS,
        ADMIN_LOGIN_RATE_LIMIT="2",
    )
    admin_auth.reset_login_rate_limiter()
    real_client = "203.0.113.44"
    with shared_rate_limiter(rate_limit_store):
        for index in range(3):
            request = _request(
                peer=RENDER_LB,
                headers=[
                    (b"x-forwarded-for", f"203.0.113.{index}, {real_client}".encode()),
                ],
            )
            admission = admin_auth.try_admit_login_attempt(
                request,
                get_settings(),
                username="ghost",
            )
            if index < 2:
                assert admission.admitted
            else:
                assert admission.throttled

    assert len(rate_limit_store.rows) == 1
    source_key = admin_auth.build_source_rate_limit_key(real_client)
    assert source_key in rate_limit_store.rows


@pytest.mark.unit
def test_privacy_telemetry_and_limiter_state_exclude_raw_addresses(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _settings_env(monkeypatch, ADMIN_TRUSTED_PROXY_CIDRS=TRUSTED_CIDRS)
    admin_auth.reset_login_rate_limiter()
    caplog.set_level(logging.INFO)
    request = _request(
        peer=RENDER_LB,
        headers=[(b"x-forwarded-for", f"{SPOOFED}, {CLIENT_A}".encode())],
    )
    with shared_rate_limiter(rate_limit_store):
        admin_auth.try_admit_login_attempt(request, get_settings(), username="ghost")

    for record in caplog.records:
        message = record.getMessage()
        assert SPOOFED not in message
        assert CLIENT_A not in message
        assert "x-forwarded-for" not in message.lower()
        if hasattr(record, "source_resolution_path"):
            assert record.source_resolution_path in {
                PATH_XFF_TRUSTED_WALK,
                PATH_DIRECT_PEER,
                PATH_UNTRUSTED_HEADERS_IGNORED,
                PATH_FORWARDED_RFC7239,
                PATH_CF_CONNECTING_IP_VERIFIED,
                PATH_MALFORMED_CHAIN,
            }

    for row_key in rate_limit_store.rows:
        assert SPOOFED not in row_key
        assert CLIENT_A not in row_key
        assert len(row_key) == 64


@pytest.mark.unit
def test_malformed_xff_with_trusted_peer_resolves_unknown(trusted_env: None) -> None:
    settings = get_settings()
    request = _request(
        peer=RENDER_LB,
        headers=[(b"x-forwarded-for", b"203.0.113.1, not-an-ip")],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "unknown"
    assert resolution.path == PATH_MALFORMED_CHAIN


@pytest.mark.unit
def test_cf_connecting_ip_requires_verified_cloudflare_hop(trusted_env: None) -> None:
    settings = get_settings()
    request = _request(
        peer=RENDER_LB,
        headers=[
            (b"cf-connecting-ip", CLIENT_A.encode()),
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "unknown"

    request_with_cf_hop = _request(
        peer=RENDER_LB,
        headers=[
            (b"x-forwarded-for", f"{CLIENT_A}, {CLOUDFLARE_EDGE}".encode()),
            (b"cf-connecting-ip", CLIENT_B.encode()),
        ],
    )
    resolution_verified = resolve_admin_login_client_source(request_with_cf_hop, settings)
    assert resolution_verified.source == CLIENT_A
    assert resolution_verified.path == PATH_XFF_TRUSTED_WALK


def _wait_for_health(base_url: str, *, attempts: int = 40) -> None:
    last_error: Exception | None = None
    for _ in range(attempts):
        try:
            with urllib.request.urlopen(f"{base_url}/health", timeout=2) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
        time.sleep(0.25)
    raise AssertionError(f"server did not become ready: {last_error}")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.mark.integration
def test_uvicorn_start_command_with_forwarded_allow_ips() -> None:
    """Confirm the render.yaml uvicorn forwarded-allow-ips flag starts and serves traffic."""
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    env = {
        **os.environ,
        "DATABASE_URL": "",
        "BASE_URL": base,
        "UVICORN_FORWARDED_ALLOW_IPS": "127.0.0.1",
    }
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--forwarded-allow-ips",
            env["UVICORN_FORWARDED_ALLOW_IPS"],
        ],
        cwd=str(Path(__file__).resolve().parents[1]),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_for_health(base)
        req = urllib.request.Request(
            f"{base}/health",
            headers={"X-Forwarded-For": "203.0.113.99"},
        )
        with urllib.request.urlopen(req) as response:
            assert response.status == 200
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


@pytest.mark.integration
def test_uvicorn_proxy_headers_middleware_does_not_bypass_limiter_walk(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ProxyHeadersMiddleware may rewrite scope client; the resolver still right-walks XFF."""
    from starlette.testclient import TestClient
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    from app.main import app

    _settings_env(
        monkeypatch,
        ADMIN_TRUSTED_PROXY_CIDRS="127.0.0.1/32",
        ADMIN_LOGIN_RATE_LIMIT="2",
    )
    wrapped = ProxyHeadersMiddleware(app, trusted_hosts="127.0.0.1")
    proxy_client = TestClient(wrapped, follow_redirects=False)
    admin_auth.reset_login_rate_limiter()
    real_client = "203.0.113.99"
    with shared_rate_limiter(rate_limit_store):
        for index in range(3):
            request = _request(
                peer="127.0.0.1",
                headers=[
                    (b"x-forwarded-for", f"203.0.113.{index}, {real_client}".encode()),
                ],
            )
            admission = admin_auth.try_admit_login_attempt(
                request,
                get_settings(),
                username="ghost",
            )
            if index < 2:
                assert admission.admitted
            else:
                assert admission.throttled
    assert len(rate_limit_store.rows) == 1
    assert admin_auth.build_source_rate_limit_key(real_client) in rate_limit_store.rows
    assert proxy_client.get("/health").status_code == 200
