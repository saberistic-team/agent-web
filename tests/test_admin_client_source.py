"""Tests for trusted-proxy admin login client source resolution (#239)."""

from __future__ import annotations

import logging
import socket
import threading
import time
from pathlib import Path
from typing import Generator

import httpx
import pytest
import uvicorn
from fastapi import Request

from app import admin_auth
from app.client_source import (
    SourceResolutionPath,
    deployment_trust_summary,
    normalize_ip_address,
    parse_x_forwarded_for,
    resolve_client_source,
)
from app.config import get_settings
from app.main import app, health

REPO_ROOT = Path(__file__).resolve().parents[1]
RENDER_YAML = REPO_ROOT / "render.yaml"
ADMIN_AUTH_DOC = REPO_ROOT / "docs" / "ADMIN_AUTH.md"

RENDER_PROXY = "10.0.0.1"
CLOUDFLARE_EDGE = "198.51.100.1"
CLIENT_A = "203.0.113.10"
CLIENT_B = "203.0.113.20"
ATTACKER_SPOOF = "203.0.113.99"
DIRECT_ATTACKER = "198.51.100.50"


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


def _trusted_cidrs(*entries: str) -> tuple[str, ...]:
    return entries


@pytest.fixture(autouse=True)
def clear_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    monkeypatch.delenv("UVICORN_PROXY_HEADERS", raising=False)
    monkeypatch.delenv("UVICORN_FORWARDED_ALLOW_IPS", raising=False)


@pytest.mark.unit
def test_wildcard_peer_boundary_still_rejects_leftmost_spoof() -> None:
    result = resolve_client_source(
        peer_host=RENDER_PROXY,
        trusted_proxy_cidrs=_trusted_cidrs("*"),
        x_forwarded_for=f"{ATTACKER_SPOOF}, {DIRECT_ATTACKER}, {RENDER_PROXY}",
    )
    assert result.source == DIRECT_ATTACKER
    assert result.path is SourceResolutionPath.TRUSTED_XFF_CHAIN


@pytest.mark.unit
def test_direct_spoof_single_and_multi_value_xff_ignored() -> None:
    for header_value in (
        ATTACKER_SPOOF,
        f"{ATTACKER_SPOOF}, {CLIENT_A}",
    ):
        result = resolve_client_source(
            peer_host=DIRECT_ATTACKER,
            trusted_proxy_cidrs=(),
            x_forwarded_for=header_value,
        )
        assert result.source == DIRECT_ATTACKER
        assert result.path is SourceResolutionPath.DIRECT_PEER


@pytest.mark.unit
def test_cloudflare_append_behavior_ignores_leftmost_spoof() -> None:
    result = resolve_client_source(
        peer_host=RENDER_PROXY,
        trusted_proxy_cidrs=_trusted_cidrs(RENDER_PROXY, CLOUDFLARE_EDGE),
        x_forwarded_for=(
            f"{ATTACKER_SPOOF}, {DIRECT_ATTACKER}, {CLOUDFLARE_EDGE}, {RENDER_PROXY}"
        ),
    )
    assert result.source == DIRECT_ATTACKER
    assert result.path is SourceResolutionPath.TRUSTED_XFF_CHAIN


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client() -> None:
    result = resolve_client_source(
        peer_host=RENDER_PROXY,
        trusted_proxy_cidrs=_trusted_cidrs(RENDER_PROXY, CLOUDFLARE_EDGE),
        x_forwarded_for=f"{CLIENT_A}, {CLOUDFLARE_EDGE}",
    )
    assert result.source == CLIENT_A
    assert result.path is SourceResolutionPath.TRUSTED_XFF_CHAIN


@pytest.mark.unit
def test_partial_trust_fails_closed_behind_untrusted_intermediary() -> None:
    untrusted_intermediary = "203.0.113.5"
    result = resolve_client_source(
        peer_host=untrusted_intermediary,
        trusted_proxy_cidrs=_trusted_cidrs(RENDER_PROXY),
        x_forwarded_for=f"{CLIENT_A}, {RENDER_PROXY}",
    )
    assert result.source == untrusted_intermediary
    assert result.path is SourceResolutionPath.DIRECT_PEER


@pytest.mark.unit
def test_direct_render_origin_ignores_cloudflare_vendor_header() -> None:
    result = resolve_client_source(
        peer_host=DIRECT_ATTACKER,
        trusted_proxy_cidrs=_trusted_cidrs(RENDER_PROXY),
        cf_connecting_ip=CLIENT_A,
        x_forwarded_for=CLIENT_A,
    )
    assert result.source == DIRECT_ATTACKER
    assert result.path is SourceResolutionPath.DIRECT_PEER


@pytest.mark.unit
def test_header_precedence_prefers_xff_over_forwarded_and_cf() -> None:
    result = resolve_client_source(
        peer_host=RENDER_PROXY,
        trusted_proxy_cidrs=_trusted_cidrs(RENDER_PROXY),
        x_forwarded_for=f"{CLIENT_A}, {RENDER_PROXY}",
        forwarded=f'for={CLIENT_B};proto=https',
        cf_connecting_ip=CLIENT_B,
    )
    assert result.source == CLIENT_A
    assert result.path is SourceResolutionPath.TRUSTED_XFF_CHAIN


@pytest.mark.unit
def test_cf_connecting_ip_used_only_when_it_matches_xff_resolution() -> None:
    result = resolve_client_source(
        peer_host=RENDER_PROXY,
        trusted_proxy_cidrs=_trusted_cidrs(RENDER_PROXY),
        x_forwarded_for=f"{CLIENT_A}, {RENDER_PROXY}",
        cf_connecting_ip=CLIENT_A,
    )
    assert result.source == CLIENT_A
    assert result.path is SourceResolutionPath.TRUSTED_CF_CONNECTING_IP


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("203.0.113.1", "203.0.113.1"),
        ("2001:db8::1", "2001:db8::1"),
        ("203.0.113.1:443", "203.0.113.1"),
        ("[2001:db8::1]:443", "2001:db8::1"),
        ("::ffff:203.0.113.1", "203.0.113.1"),
        ("  203.0.113.1  ", "203.0.113.1"),
        ("", None),
        ("not-an-ip", None),
    ],
)
def test_address_normalization(raw: str, expected: str | None) -> None:
    assert normalize_ip_address(raw) == expected


@pytest.mark.unit
def test_xff_rejects_empty_elements_and_overlong_chain() -> None:
    assert parse_x_forwarded_for("203.0.113.1,,203.0.113.2") is None
    long_chain = ", ".join(f"203.0.113.{index}" for index in range(40))
    assert parse_x_forwarded_for(long_chain) is None


@pytest.mark.unit
def test_client_ip_wrapper_uses_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", RENDER_PROXY)
    settings = get_settings()
    request = _request_with_client(
        RENDER_PROXY,
        headers=[(b"x-forwarded-for", f"{CLIENT_A}, {RENDER_PROXY}".encode())],
    )
    assert admin_auth.client_ip(request, settings) == CLIENT_A


@pytest.mark.unit
def test_health_reports_deployment_trust_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", "*")
    monkeypatch.setenv("UVICORN_PROXY_HEADERS", "true")
    monkeypatch.setenv("UVICORN_FORWARDED_ALLOW_IPS", "*")
    payload = health()
    trust = payload["admin_login_source_trust"]
    assert trust["trusted_proxies_configured"] is True
    assert trust["trust_wildcard"] is True
    assert trust["uvicorn_proxy_headers"] is True
    assert trust["uvicorn_forwarded_allow_ips"] == "*"
    assert trust["resolution_mode"] == "trusted_hop_chain"


@pytest.mark.unit
def test_deployment_trust_summary_without_proxies() -> None:
    summary = deployment_trust_summary(
        trusted_proxy_cidrs=(),
        uvicorn_proxy_headers=False,
        uvicorn_forwarded_allow_ips="127.0.0.1",
    )
    assert summary["trusted_proxies_configured"] is False
    assert summary["resolution_mode"] == "direct_peer"


@pytest.mark.unit
def test_render_yaml_proxy_settings_are_consistent() -> None:
    text = RENDER_YAML.read_text(encoding="utf-8")
    assert "--proxy-headers" in text
    assert "--forwarded-allow-ips='*'" in text
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in text
    assert 'value: "*"' in text
    assert "UVICORN_PROXY_HEADERS" in text
    assert "UVICORN_FORWARDED_ALLOW_IPS" in text


@pytest.mark.unit
def test_admin_auth_doc_documents_trusted_hop_model() -> None:
    text = ADMIN_AUTH_DOC.read_text(encoding="utf-8")
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in text
    assert "right-to-left" in text
    assert "admin_login_source_trust" in text
    assert "rollback" in text.lower()


@pytest.mark.unit
def test_telemetry_logs_path_without_raw_addresses(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="app.client_source")
    result = resolve_client_source(
        peer_host=DIRECT_ATTACKER,
        trusted_proxy_cidrs=(),
        x_forwarded_for=ATTACKER_SPOOF,
    )
    from app.client_source import record_client_source_telemetry

    record_client_source_telemetry(result)
    messages = " ".join(record.message for record in caplog.records)
    assert "source_resolution_path" not in messages
    assert ATTACKER_SPOOF not in messages
    assert DIRECT_ATTACKER not in messages
    assert any(
        getattr(record, "source_resolution_path", None)
        == SourceResolutionPath.DIRECT_PEER.value
        for record in caplog.records
        if record.name == "app.client_source"
    )


@pytest.mark.unit
@pytest.mark.integration
def test_rotating_spoofed_headers_do_not_create_new_limiter_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_admin_auth import FakeRateLimitStore, shared_rate_limiter

    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", RENDER_PROXY)
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    settings = get_settings()
    store = FakeRateLimitStore()
    with shared_rate_limiter(store):
        for index in range(4):
            request = _request_with_client(
                RENDER_PROXY,
                headers=[
                    (
                        b"x-forwarded-for",
                        (
                            f"203.0.113.{index}, {DIRECT_ATTACKER}, {RENDER_PROXY}"
                        ).encode(),
                    )
                ],
            )
            admission = admin_auth.try_admit_login_attempt(
                request,
                settings,
                username=f"user-{index}",
            )
            if index < 2:
                assert admission.admitted is True
            else:
                assert admission.throttled is True

    assert len(store.rows) == 1


def _reserve_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def uvicorn_proxy_server(monkeypatch: pytest.MonkeyPatch) -> Generator[str, None, None]:
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("ADMIN_USERNAME", "operator")
    monkeypatch.setenv(
        "ADMIN_PASSWORD_HASH",
        "$argon2id$v=19$m=65536,t=3,p=4$test$test",
    )
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "test-session-secret-32chars-minimum")
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", "127.0.0.1")
    monkeypatch.setenv("UVICORN_PROXY_HEADERS", "true")
    monkeypatch.setenv("UVICORN_FORWARDED_ALLOW_IPS", "127.0.0.1")

    port = _reserve_port()
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    origin = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 5
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
        pytest.fail("uvicorn server failed to start")

    try:
        yield origin
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@pytest.mark.integration
def test_uvicorn_proxy_configuration_resolves_trusted_xff_chain(
    uvicorn_proxy_server: str,
) -> None:
    with httpx.Client() as client:
        health_response = client.get(f"{uvicorn_proxy_server}/health")
        assert health_response.status_code == 200
        trust = health_response.json()["admin_login_source_trust"]
        assert trust["uvicorn_proxy_headers"] is True
        assert trust["resolution_mode"] == "trusted_hop_chain"

        scope_request = _request_with_client(
            "127.0.0.1",
            headers=[(b"x-forwarded-for", f"{CLIENT_A}, 127.0.0.1".encode())],
        )
        settings = get_settings()
        assert admin_auth.client_ip(scope_request, settings) == CLIENT_A


@pytest.mark.unit
def test_limiter_rows_store_digests_not_raw_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_admin_auth import FakeRateLimitStore, shared_rate_limiter

    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", RENDER_PROXY)
    store = FakeRateLimitStore()
    source = resolve_client_source(
        peer_host=RENDER_PROXY,
        trusted_proxy_cidrs=(RENDER_PROXY,),
        x_forwarded_for=f"{CLIENT_A}, {RENDER_PROXY}",
    ).source
    key = admin_auth.build_source_rate_limit_key(source)
    with shared_rate_limiter(store):
        request = _request_with_client(
            RENDER_PROXY,
            headers=[(b"x-forwarded-for", f"{CLIENT_A}, {RENDER_PROXY}".encode())],
        )
        admission = admin_auth.try_admit_login_attempt(request, get_settings())
    assert admission.admitted is True
    assert key in store.rows
    serialized = str(store.rows)
    assert CLIENT_A not in serialized
    assert RENDER_PROXY not in serialized
    assert "x-forwarded-for" not in serialized.lower()
