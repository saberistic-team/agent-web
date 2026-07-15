"""Tests for trusted-proxy admin login client source resolution (#239)."""

from __future__ import annotations

import logging
import socket
import threading
import time
from contextlib import contextmanager
from typing import Any, Generator

import httpx
import pytest
import uvicorn
from fastapi import Request
from unittest.mock import patch

from app import admin_auth
from app.admin_client_source import (
    SourceResolutionPath,
    reset_client_source_telemetry_for_tests,
    resolve_admin_login_client_source,
)
from app.network_utils import normalize_ip
from app.config import get_settings

RENDER_TRUSTED_CIDRS = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.1/32,::1/128"
RENDER_LB = "10.0.0.5"
CF_EDGE = "173.245.48.10"
REAL_CLIENT = "203.0.113.50"
ATTACKER_SPOOF = "203.0.113.99"
DIRECT_ATTACKER = "198.51.100.10"


def _request(
    *,
    peer: str | None,
    headers: list[tuple[bytes, bytes]] | None = None,
) -> Request:
    scope: dict[str, Any] = {
        "type": "http",
        "headers": headers or [],
        "method": "POST",
        "path": "/admin/login",
    }
    if peer is not None:
        scope["client"] = (peer, 12345)
    return Request(scope)


def _trusted_settings(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", RENDER_TRUSTED_CIDRS)
    return get_settings()


def _add_header(request: Request, name: str, value: str) -> None:
    request.headers.__dict__["_list"].append((name.lower().encode(), value.encode()))


@pytest.fixture(autouse=True)
def _reset_telemetry() -> None:
    reset_client_source_telemetry_for_tests()


@pytest.mark.unit
def test_normalize_ip_formats() -> None:
    assert normalize_ip("203.0.113.1") == "203.0.113.1"
    assert normalize_ip("203.0.113.1:443") == "203.0.113.1"
    assert normalize_ip(" 2001:db8::1 ") == "2001:db8::1"
    assert normalize_ip("[2001:db8::1]:443") == "2001:db8::1"
    assert normalize_ip("::ffff:203.0.113.1") == "203.0.113.1"
    assert normalize_ip("") is None
    assert normalize_ip("not-an-ip") is None


@pytest.mark.unit
def test_direct_spoof_single_and_multi_value_xff_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    for header_value in (ATTACKER_SPOOF, f"{ATTACKER_SPOOF}, {ATTACKER_SPOOF}"):
        request = _request(peer=DIRECT_ATTACKER)
        _add_header(request, "x-forwarded-for", header_value)
        resolution = resolve_admin_login_client_source(request, settings)
        assert resolution.source == DIRECT_ATTACKER
        assert resolution.path is SourceResolutionPath.DIRECT_PEER


@pytest.mark.unit
def test_cloudflare_append_ignores_attacker_leftmost_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _trusted_settings(monkeypatch)
    request = _request(peer=RENDER_LB)
    _add_header(
        request,
        "x-forwarded-for",
        f"{ATTACKER_SPOOF}, {REAL_CLIENT}, {CF_EDGE}, {RENDER_LB}",
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
    assert resolution.path is SourceResolutionPath.TRUSTED_XFF


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _trusted_settings(monkeypatch)
    request = _request(peer=RENDER_LB)
    _add_header(
        request,
        "x-forwarded-for",
        f"{REAL_CLIENT}, {CF_EDGE}, {RENDER_LB}",
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
    assert resolution.path is SourceResolutionPath.TRUSTED_XFF


@pytest.mark.unit
def test_partial_trust_untrusted_intermediary_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _trusted_settings(monkeypatch)
    request = _request(peer=DIRECT_ATTACKER)
    _add_header(
        request,
        "x-forwarded-for",
        f"{REAL_CLIENT}, {RENDER_LB}",
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == DIRECT_ATTACKER
    assert resolution.path is SourceResolutionPath.UNTRUSTED_PEER


@pytest.mark.unit
def test_direct_render_origin_ignores_cloudflare_vendor_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _trusted_settings(monkeypatch)
    request = _request(peer=DIRECT_ATTACKER)
    _add_header(request, "cf-connecting-ip", REAL_CLIENT)
    _add_header(request, "x-forwarded-for", REAL_CLIENT)
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == DIRECT_ATTACKER
    assert resolution.path is SourceResolutionPath.UNTRUSTED_PEER


@pytest.mark.unit
def test_header_precedence_xff_before_forwarded_and_cf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _trusted_settings(monkeypatch)
    request = _request(peer=RENDER_LB)
    _add_header(request, "x-forwarded-for", f"{REAL_CLIENT}, {RENDER_LB}")
    _add_header(request, "forwarded", f'for="{ATTACKER_SPOOF}"')
    _add_header(request, "cf-connecting-ip", ATTACKER_SPOOF)
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
    assert resolution.path is SourceResolutionPath.TRUSTED_XFF


@pytest.mark.unit
def test_forwarded_header_used_when_xff_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _trusted_settings(monkeypatch)
    request = _request(peer=RENDER_LB)
    _add_header(request, "forwarded", f'for="{REAL_CLIENT}", for={RENDER_LB}')
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
    assert resolution.path is SourceResolutionPath.TRUSTED_FORWARDED


@pytest.mark.unit
def test_cf_connecting_ip_used_when_only_vendor_header_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _trusted_settings(monkeypatch)
    request = _request(peer=RENDER_LB)
    _add_header(request, "cf-connecting-ip", REAL_CLIENT)
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
    assert resolution.path is SourceResolutionPath.TRUSTED_CF_CONNECTING


@pytest.mark.unit
def test_address_format_edge_cases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _trusted_settings(monkeypatch)
    request = _request(peer=RENDER_LB)
    _add_header(
        request,
        "x-forwarded-for",
        f" ::ffff:203.0.113.1 , {CF_EDGE}, {RENDER_LB} ",
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.1"

    empty_elements = _request(peer=RENDER_LB)
    _add_header(empty_elements, "x-forwarded-for", f", , {REAL_CLIENT}, {RENDER_LB}")
    assert resolve_admin_login_client_source(empty_elements, settings).source == REAL_CLIENT

    invalid = _request(peer=RENDER_LB)
    _add_header(invalid, "x-forwarded-for", f"not-an-ip, {RENDER_LB}")
    assert (
        resolve_admin_login_client_source(invalid, settings).path
        is SourceResolutionPath.INVALID_FORWARDING
    )

    overlong = _request(peer=RENDER_LB)
    chain = ", ".join([f"203.0.113.{index}" for index in range(12)] + [RENDER_LB])
    _add_header(overlong, "x-forwarded-for", chain)
    assert (
        resolve_admin_login_client_source(overlong, settings).path
        is SourceResolutionPath.INVALID_FORWARDING
    )


@pytest.mark.unit
def test_missing_peer_resolves_to_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _trusted_settings(monkeypatch)
    request = _request(peer=None)
    _add_header(request, "x-forwarded-for", REAL_CLIENT)
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "unknown"
    assert resolution.path is SourceResolutionPath.MISSING_PEER


@pytest.mark.unit
def test_duplicate_xff_header_lines_are_combined_not_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Duplicate header *lines* (not one comma-joined value) must not hide hops."""
    settings = _trusted_settings(monkeypatch)
    request = _request(peer=RENDER_LB)
    _add_header(request, "x-forwarded-for", ATTACKER_SPOOF)
    _add_header(request, "x-forwarded-for", f"{REAL_CLIENT}, {CF_EDGE}, {RENDER_LB}")
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
    assert resolution.path is SourceResolutionPath.TRUSTED_XFF


@pytest.mark.unit
def test_duplicate_cf_connecting_ip_headers_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _trusted_settings(monkeypatch)
    request = _request(peer=RENDER_LB)
    _add_header(request, "cf-connecting-ip", REAL_CLIENT)
    _add_header(request, "cf-connecting-ip", ATTACKER_SPOOF)
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.path is SourceResolutionPath.INVALID_FORWARDING
    assert resolution.source == "unknown"


@pytest.mark.unit
def test_rotating_spoofed_headers_share_one_limiter_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_admin_auth import FakeRateLimitStore, shared_rate_limiter

    store = FakeRateLimitStore()
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", RENDER_TRUSTED_CIDRS)
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "3")

    with shared_rate_limiter(store):
        for index in range(4):
            request = _request(peer=RENDER_LB)
            _add_header(
                request,
                "x-forwarded-for",
                f"203.0.113.{index}, {REAL_CLIENT}, {RENDER_LB}",
            )
            settings = get_settings()
            admission = admin_auth.try_admit_login_attempt(
                request,
                settings,
                username=f"user-{index}",
            )
            if index < 3:
                assert admission.admitted
            else:
                assert admission.throttled

    source_key = admin_auth.build_source_rate_limit_key(REAL_CLIENT)
    assert len(store.rows) == 1
    assert source_key in store.rows


@pytest.mark.unit
def test_telemetry_and_limiter_state_contain_no_raw_forwarding_data(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = _trusted_settings(monkeypatch)
    request = _request(peer=RENDER_LB)
    _add_header(request, "x-forwarded-for", f"not-an-ip, {RENDER_LB}")
    with caplog.at_level(logging.INFO):
        resolve_admin_login_client_source(request, settings)
    logged = " ".join(record.message for record in caplog.records)
    assert REAL_CLIENT not in logged
    assert "x-forwarded-for" not in logged.lower()
    assert "203.0.113" not in logged

    resolution = resolve_admin_login_client_source(request, settings)
    limiter_key = admin_auth.build_source_rate_limit_key(resolution.source)
    assert REAL_CLIENT not in limiter_key
    assert ATTACKER_SPOOF not in limiter_key
    assert len(limiter_key) == 64


@contextmanager
def _uvicorn_server(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[str, None, None]:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", "operator")
    monkeypatch.setenv(
        "ADMIN_PASSWORD_HASH",
        "$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$RdescudvJCsgt3ub+b+dWRWJTmaaJObG",
    )
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "test-session-secret-32chars-minimum")
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", RENDER_TRUSTED_CIDRS)
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    config = uvicorn.Config(
        "app.main:app",
        host="127.0.0.1",
        port=port,
        log_level="warning",
    )
    with patch("app.main.db.init_db"):
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()

        deadline = time.time() + 5
        while not server.started and time.time() < deadline:
            time.sleep(0.05)
        assert server.started

        try:
            yield f"http://127.0.0.1:{port}"
        finally:
            server.should_exit = True
            thread.join(timeout=5)


@pytest.mark.integration
def test_uvicorn_server_matches_deployment_and_trusted_peer_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live Uvicorn (production start args) with trusted 127.0.0.1 peer resolution."""
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", RENDER_TRUSTED_CIDRS)

    with _uvicorn_server(monkeypatch) as base_url:
        with httpx.Client(base_url=base_url, timeout=3.0) as client:
            health = client.get("/health")
            assert health.status_code == 200
            assert health.json()["status"] == "ok"

    settings = get_settings()
    request = _request(peer="127.0.0.1")
    _add_header(
        request,
        "x-forwarded-for",
        f"{ATTACKER_SPOOF}, {REAL_CLIENT}, 127.0.0.1",
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
    assert resolution.path is SourceResolutionPath.TRUSTED_XFF
