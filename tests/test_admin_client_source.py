"""Tests for trusted-proxy admin login client source resolution (#239)."""

from __future__ import annotations

import logging
import socket
import threading
import time
from contextlib import contextmanager
from typing import Generator
from unittest.mock import patch

import httpx
import pytest
import uvicorn
from fastapi import Request
from fastapi.testclient import TestClient

from app import admin_auth
from app.client_source import resolve_admin_login_client_source
from app.config import get_settings
from app.main import app
from app.proxy_networks import (
    DEFAULT_TRUSTED_FORWARDER_IPS,
    DEFAULT_TRUSTED_PROXY_IPS,
    format_normalized_ip,
    normalize_ip_address,
)
from tests.test_admin_auth import (
    TEST_HASH,
    TEST_SECRET,
    TEST_USERNAME,
    FakeRateLimitStore,
    _parse_login_form,
    mock_db_connection,
    shared_rate_limiter,
)


@pytest.fixture
def rate_limit_store() -> FakeRateLimitStore:
    return FakeRateLimitStore()

RENDER_PROXY = "10.21.157.68"
CLOUDFLARE_EDGE = "104.22.17.40"
CLIENT_A = "203.0.113.50"
CLIENT_B = "203.0.113.77"
SPOOFED_LEFT = "198.51.100.99"


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


def _trusted_settings(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", ",".join(DEFAULT_TRUSTED_PROXY_IPS))
    monkeypatch.setenv("ADMIN_TRUSTED_FORWARDER_IPS", ",".join(DEFAULT_TRUSTED_FORWARDER_IPS))
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    return get_settings()


@pytest.fixture
def trusted_proxy_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", ",".join(DEFAULT_TRUSTED_PROXY_IPS))
    monkeypatch.setenv("ADMIN_TRUSTED_FORWARDER_IPS", ",".join(DEFAULT_TRUSTED_FORWARDER_IPS))
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    admin_auth.reset_login_rate_limiter()
    return TestClient(app, follow_redirects=False, client=(RENDER_PROXY, 50000))


@pytest.mark.unit
def test_direct_spoof_single_and_multi_value_xff_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    for header_value in (
        SPOOFED_LEFT,
        f"{SPOOFED_LEFT}, {CLIENT_A}",
    ):
        request = _request_with_client(
            "198.51.100.10",
            headers=[(b"x-forwarded-for", header_value.encode("ascii"))],
        )
        resolution = resolve_admin_login_client_source(request, settings)
        assert resolution.source == "198.51.100.10"
        assert resolution.path == "direct_peer"


@pytest.mark.unit
def test_cloudflare_append_ignores_attacker_leftmost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _trusted_settings(monkeypatch)
    xff = f"{SPOOFED_LEFT}, {CLIENT_A}, {CLOUDFLARE_EDGE}"
    request = _request_with_client(
        RENDER_PROXY,
        headers=[(b"x-forwarded-for", xff.encode("ascii"))],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == CLIENT_A
    assert resolution.path == "xff_right_to_left"


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _trusted_settings(monkeypatch)
    xff = f"{CLIENT_B}, {CLOUDFLARE_EDGE}"
    request = _request_with_client(
        RENDER_PROXY,
        headers=[(b"x-forwarded-for", xff.encode("ascii"))],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == CLIENT_B
    assert resolution.path == "xff_right_to_left"


@pytest.mark.unit
def test_partial_trust_untrusted_intermediary_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _trusted_settings(monkeypatch)
    untrusted_relay = "203.0.113.200"
    xff = f"{CLIENT_A}, {CLOUDFLARE_EDGE}"
    request = _request_with_client(
        untrusted_relay,
        headers=[(b"x-forwarded-for", xff.encode("ascii"))],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == untrusted_relay
    assert resolution.path == "direct_peer"


@pytest.mark.unit
def test_direct_render_origin_ignores_spoofed_cf_connecting_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    request = _request_with_client(
        "203.0.113.5",
        headers=[
            (b"cf-connecting-ip", CLIENT_A.encode("ascii")),
            (b"x-forwarded-for", CLIENT_A.encode("ascii")),
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.5"
    assert resolution.path == "direct_peer"


@pytest.mark.unit
def test_cf_connecting_ip_used_when_cloudflare_hop_proven(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _trusted_settings(monkeypatch)
    xff = f"{CLIENT_A}, {CLOUDFLARE_EDGE}"
    request = _request_with_client(
        RENDER_PROXY,
        headers=[
            (b"x-forwarded-for", xff.encode("ascii")),
            (b"cf-connecting-ip", CLIENT_A.encode("ascii")),
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == CLIENT_A
    assert resolution.path == "cf_connecting_ip"


@pytest.mark.unit
def test_header_precedence_cf_over_conflicting_forwarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _trusted_settings(monkeypatch)
    xff = f"{CLIENT_A}, {CLOUDFLARE_EDGE}"
    forwarded = f'for="{SPOOFED_LEFT}";proto=https, for="{CLOUDFLARE_EDGE}"'
    request = _request_with_client(
        RENDER_PROXY,
        headers=[
            (b"x-forwarded-for", xff.encode("ascii")),
            (b"cf-connecting-ip", CLIENT_A.encode("ascii")),
            (b"forwarded", forwarded.encode("ascii")),
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == CLIENT_A
    assert resolution.path == "cf_connecting_ip"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("203.0.113.1", "203.0.113.1"),
        ("2001:db8::1", "2001:db8::1"),
        ("203.0.113.1:443", "203.0.113.1"),
        ("::ffff:203.0.113.1", "203.0.113.1"),
        ("  203.0.113.1  ", "203.0.113.1"),
        ("", None),
        ("not-an-ip", None),
    ],
)
def test_address_normalization(raw: str, expected: str | None) -> None:
    parsed = normalize_ip_address(raw)
    if expected is None:
        assert parsed is None
    else:
        assert format_normalized_ip(parsed) == expected


@pytest.mark.unit
def test_overlong_xff_chain_falls_back_to_peer(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _trusted_settings(monkeypatch)
    chain = ", ".join(f"203.0.113.{index}" for index in range(25))
    request = _request_with_client(
        RENDER_PROXY,
        headers=[(b"x-forwarded-for", chain.encode("ascii"))],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == RENDER_PROXY
    assert resolution.path == "trusted_peer_fallback"
    assert resolution.invalid_forwarding is True


@pytest.mark.unit
def test_invalid_forwarding_emits_sampled_telemetry(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = _trusted_settings(monkeypatch)
    request = _request_with_client(
        RENDER_PROXY,
        headers=[(b"x-forwarded-for", b"not-an-ip")],
    )
    caplog.set_level(logging.WARNING)
    import app.client_source as client_source_module

    client_source_module._invalid_forwarding_last_logged_at = 0.0
    with patch.object(client_source_module, "_INVALID_FORWARDING_LOG_INTERVAL_SECONDS", 60.0):
        resolve_admin_login_client_source(request, settings)
        resolve_admin_login_client_source(request, settings)
    warnings = [
        record
        for record in caplog.records
        if record.message == "Admin login client source rejected forwarding headers"
    ]
    assert len(warnings) == 1
    assert warnings[0].client_source_path == "trusted_peer_fallback"
    assert "203.0.113" not in warnings[0].getMessage()


@pytest.mark.unit
@pytest.mark.integration
def test_rotating_spoofed_headers_does_not_create_new_limiter_rows(
    trusted_proxy_client: TestClient,
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "3")
    with shared_rate_limiter(rate_limit_store):
        for index in range(5):
            headers = {"X-Forwarded-For": f"198.51.100.{index}, {CLIENT_A}, {CLOUDFLARE_EDGE}"}
            with mock_db_connection():
                form = trusted_proxy_client.get("/admin/login")
            csrf_token, cookies = _parse_login_form(form)
            with mock_db_connection():
                response = trusted_proxy_client.post(
                    "/admin/login",
                    data={
                        "username": "ghost",
                        "password": "wrong",
                        "csrf_token": csrf_token,
                    },
                    cookies=cookies,
                    headers=headers,
                )
            if index < 3:
                assert response.status_code == 401
            else:
                assert response.status_code == 429
    assert len(rate_limit_store.rows) == 1
    source_key = admin_auth.build_source_rate_limit_key(CLIENT_A)
    assert source_key in rate_limit_store.rows


@pytest.mark.unit
def test_privacy_limiter_keys_and_logs_exclude_raw_forwarding(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = _trusted_settings(monkeypatch)
    request = _request_with_client(
        RENDER_PROXY,
        headers=[(b"x-forwarded-for", f"{CLIENT_A}, {CLOUDFLARE_EDGE}".encode("ascii"))],
    )
    caplog.set_level(logging.DEBUG)
    source = admin_auth.client_ip(request, settings)
    key = admin_auth.build_source_rate_limit_key(source)
    assert CLIENT_A not in key
    assert CLOUDFLARE_EDGE not in key
    for record in caplog.records:
        message = record.getMessage()
        assert CLIENT_A not in message
        assert CLOUDFLARE_EDGE not in message


@pytest.mark.unit
def test_render_yaml_proxy_trust_configuration() -> None:
    from pathlib import Path

    render_yaml = Path("render.yaml").read_text(encoding="utf-8")
    assert "--forwarded-allow-ips=127.0.0.1" in render_yaml
    assert "ADMIN_TRUSTED_PROXY_IPS" in render_yaml
    assert "ADMIN_TRUSTED_FORWARDER_IPS" in render_yaml
    assert "10.0.0.0/8" in render_yaml
    assert "172.64.0.0/13" in render_yaml


@pytest.mark.unit
def test_health_reports_client_source_trust_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_IPS", raising=False)
    monkeypatch.delenv("ADMIN_TRUSTED_FORWARDER_IPS", raising=False)
    client = TestClient(app)
    payload = client.get("/health").json()
    assert payload["admin_client_source_trust"] == "direct_peer"

    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", "10.0.0.0/8")
    monkeypatch.delenv("ADMIN_TRUSTED_FORWARDER_IPS", raising=False)
    payload = client.get("/health").json()
    assert payload["admin_client_source_trust"] == "verified_proxy_chain"


@contextmanager
def _uvicorn_server() -> Generator[str, None, None]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    _host, port = sock.getsockname()
    sock.close()

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        forwarded_allow_ips="127.0.0.1",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 5
    while not server.started and time.time() < deadline:
        time.sleep(0.01)
    if not server.started:
        raise RuntimeError("uvicorn server failed to start")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@pytest.mark.integration
def test_uvicorn_integration_respects_proxy_trust_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", ",".join(DEFAULT_TRUSTED_PROXY_IPS))
    monkeypatch.setenv("ADMIN_TRUSTED_FORWARDER_IPS", ",".join(DEFAULT_TRUSTED_FORWARDER_IPS))

    direct = admin_auth.client_ip(
        _request_with_client(
            "203.0.113.5",
            headers=[(b"x-forwarded-for", f"{SPOOFED_LEFT}, {CLIENT_A}".encode())],
        ),
        get_settings(),
    )
    trusted = admin_auth.client_ip(
        _request_with_client(
            RENDER_PROXY,
            headers=[
                (
                    b"x-forwarded-for",
                    f"{SPOOFED_LEFT}, {CLIENT_A}, {CLOUDFLARE_EDGE}".encode(),
                )
            ],
        ),
        get_settings(),
    )
    assert direct == "203.0.113.5"
    assert trusted == CLIENT_A

    with _uvicorn_server() as base_url:
        health = httpx.get(f"{base_url}/health", timeout=5).json()
        assert health["admin_client_source_trust"] == "verified_proxy_chain"
