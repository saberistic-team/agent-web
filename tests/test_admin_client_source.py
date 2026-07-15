"""Tests for trusted-proxy admin login client source resolution (#239)."""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

import httpx
import pytest
from fastapi import FastAPI, Request
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app import admin_auth
from app.client_source import (
    ClientSourceResolutionPath,
    UNKNOWN_SOURCE,
    client_source_telemetry_snapshot,
    normalize_client_source,
    production_trusted_proxy_cidrs,
    reset_client_source_telemetry,
    resolve_admin_login_client_source,
)
from app.config import get_settings
from fastapi.testclient import TestClient
from tests.test_admin_auth import (
    FakeRateLimitStore,
    _login,
    admin_env,
    shared_rate_limiter,
)


@pytest.fixture
def rate_limit_store() -> FakeRateLimitStore:
    return FakeRateLimitStore()

REPO_ROOT = Path(__file__).resolve().parents[1]
RENDER_TRUSTED_CIDRS = production_trusted_proxy_cidrs()
PRODUCTION_FORWARDED_ALLOW_IPS = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"


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


def _settings_with_trusted_cidrs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    cidrs: str = RENDER_TRUSTED_CIDRS,
) -> None:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", cidrs)
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)


@pytest.fixture(autouse=True)
def reset_telemetry() -> Generator[None, None, None]:
    reset_client_source_telemetry()
    yield
    reset_client_source_telemetry()


@pytest.mark.unit
def test_normalize_client_source_formats() -> None:
    assert normalize_client_source("203.0.113.1") == "203.0.113.1"
    assert normalize_client_source("203.0.113.1:443") == "203.0.113.1"
    assert normalize_client_source(" 2001:db8::1 ") == "2001:db8::1"
    assert normalize_client_source("[2001:db8::1]:443") == "2001:db8::1"
    assert normalize_client_source("::ffff:203.0.113.9") == "203.0.113.9"
    assert normalize_client_source("") is None
    assert normalize_client_source("not-an-ip") == "not-an-ip"


@pytest.mark.unit
def test_direct_spoof_ignored_without_trusted_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    settings = get_settings()

    single = _request_with_client(
        "198.51.100.10",
        headers=[(b"x-forwarded-for", b"203.0.113.99")],
    )
    multi = _request_with_client(
        "198.51.100.10",
        headers=[(b"x-forwarded-for", b"203.0.113.1, 203.0.113.2, 198.51.100.10")],
    )

    assert resolve_admin_login_client_source(single, settings).source == "198.51.100.10"
    assert resolve_admin_login_client_source(multi, settings).source == "198.51.100.10"
    assert (
        client_source_telemetry_snapshot()[
            ClientSourceResolutionPath.UNTRUSTED_FORWARDING_IGNORED.value
        ]
        == 2
    )


@pytest.mark.unit
def test_cloudflare_append_selects_connecting_address_not_leftmost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings_with_trusted_cidrs(monkeypatch)
    settings = get_settings()
    request = _request_with_client(
        "10.0.0.5",
        headers=[
            (b"x-forwarded-for", b"203.0.113.77, 198.51.100.44"),
            (b"cf-connecting-ip", b"198.51.100.44"),
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "198.51.100.44"
    assert resolution.path is ClientSourceResolutionPath.CF_CONNECTING_IP_CONFIRMED


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings_with_trusted_cidrs(monkeypatch)
    settings = get_settings()
    request = _request_with_client(
        "10.0.0.2",
        headers=[(b"x-forwarded-for", b"203.0.113.50, 10.0.0.2")],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.50"


@pytest.mark.unit
def test_partial_trust_fails_closed_when_untrusted_intermediary_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings_with_trusted_cidrs(monkeypatch)
    settings = get_settings()
    request = _request_with_client(
        "10.0.0.2",
        headers=[(b"x-forwarded-for", b"203.0.113.9, 198.51.100.8, 10.0.0.2")],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == UNKNOWN_SOURCE
    assert resolution.path is ClientSourceResolutionPath.MALFORMED_FORWARDING


@pytest.mark.unit
def test_direct_render_origin_ignores_spoofed_cf_connecting_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings_with_trusted_cidrs(monkeypatch)
    settings = get_settings()
    request = _request_with_client(
        "10.0.0.2",
        headers=[
            (b"cf-connecting-ip", b"203.0.113.55"),
            (b"x-forwarded-for", b"203.0.113.55"),
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.55"
    assert resolution.path in {
        ClientSourceResolutionPath.FORWARDED_CHAIN,
        ClientSourceResolutionPath.CF_CONNECTING_IP_CONFIRMED,
    }

    spoof_only = _request_with_client(
        "10.0.0.2",
        headers=[(b"cf-connecting-ip", b"203.0.113.55")],
    )
    spoof_resolution = resolve_admin_login_client_source(spoof_only, settings)
    assert spoof_resolution.source == UNKNOWN_SOURCE
    assert spoof_resolution.path is ClientSourceResolutionPath.MALFORMED_FORWARDING


@pytest.mark.unit
def test_header_precedence_prefers_x_forwarded_for_over_forwarded_and_cf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings_with_trusted_cidrs(monkeypatch)
    settings = get_settings()
    request = _request_with_client(
        "10.0.0.2",
        headers=[
            (b"x-forwarded-for", b"203.0.113.10, 10.0.0.2"),
            (b"forwarded", b'for="203.0.113.99";proto=https'),
            (b"cf-connecting-ip", b"203.0.113.10"),
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.10"
    assert resolution.path is ClientSourceResolutionPath.CF_CONNECTING_IP_CONFIRMED


@pytest.mark.unit
def test_forwarded_rfc7239_used_when_xff_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings_with_trusted_cidrs(monkeypatch)
    settings = get_settings()
    request = _request_with_client(
        "10.0.0.2",
        headers=[(b"forwarded", b'for="203.0.113.61", for=10.0.0.2;proto=https')],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.61"
    assert resolution.path is ClientSourceResolutionPath.FORWARDED_RFC7239


@pytest.mark.unit
@pytest.mark.parametrize(
    ("header_value", "expected"),
    [
        ("", UNKNOWN_SOURCE),
        (" , ", UNKNOWN_SOURCE),
        ("not-an-ip", UNKNOWN_SOURCE),
        ("203.0.113.1, , 10.0.0.2", UNKNOWN_SOURCE),
        (",".join([f"203.0.113.{index}" for index in range(40)]), UNKNOWN_SOURCE),
    ],
)
def test_malformed_and_overlong_forwarding_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    header_value: str,
    expected: str,
) -> None:
    _settings_with_trusted_cidrs(monkeypatch)
    settings = get_settings()
    request = _request_with_client(
        "10.0.0.2",
        headers=[(b"x-forwarded-for", header_value.encode("ascii"))],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == expected
    assert resolution.path is ClientSourceResolutionPath.MALFORMED_FORWARDING


@pytest.mark.unit
def test_client_ip_wrapper_uses_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    settings = get_settings()
    request = _request_with_client(
        "198.51.100.10",
        headers=[(b"x-forwarded-for", b"203.0.113.99")],
    )
    assert admin_auth.client_ip(request, settings) == "198.51.100.10"


@pytest.mark.unit
@pytest.mark.integration
def test_rotating_spoofed_headers_do_not_create_new_source_buckets(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
    admin_env: None,
) -> None:
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    with shared_rate_limiter(rate_limit_store):
        for index in range(5):
            response = _login(
                username="ghost",
                password="wrong",
                headers={"X-Forwarded-For": f"203.0.113.{index}"},
            )
            if index < 2:
                assert response.status_code == 401
            else:
                assert response.status_code == 429

    source_key = admin_auth.build_source_rate_limit_key("testclient")
    assert len(rate_limit_store.rows) == 1
    assert source_key in rate_limit_store.rows


@pytest.mark.unit
def test_telemetry_contains_no_raw_addresses(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    settings = get_settings()
    request = _request_with_client(
        "198.51.100.10",
        headers=[(b"x-forwarded-for", b"203.0.113.99")],
    )
    admin_auth.client_ip(request, settings)

    snapshot = client_source_telemetry_snapshot()
    assert snapshot
    serialized = repr(snapshot)
    assert "203.0.113.99" not in serialized
    assert "198.51.100.10" not in serialized

    for record in caplog.records:
        message = record.getMessage()
        assert "203.0.113.99" not in message
        assert "198.51.100.10" not in message
        if hasattr(record, "resolution_path"):
            assert "203.0.113" not in str(record.resolution_path)


@pytest.mark.unit
def test_rate_limit_rows_store_digests_only(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
    admin_env: None,
) -> None:
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "1")
    with shared_rate_limiter(rate_limit_store):
        response = _login(
            password="wrong",
            headers={"X-Forwarded-For": "203.0.113.44"},
        )
    assert response.status_code == 401
    for key in rate_limit_store.rows:
        assert "203.0.113" not in key
        assert len(key) == 64


@pytest.mark.unit
def test_deployment_proxy_settings_are_consistent() -> None:
    render_yaml = (REPO_ROOT / "render.yaml").read_text(encoding="utf-8")
    start_match = re.search(r"startCommand:\s*(.+)$", render_yaml, re.MULTILINE)
    assert start_match is not None
    start_command = start_match.group(1)

    assert "--proxy-headers" in start_command
    allow_match = re.search(r"--forwarded-allow-ips=([^\s]+)", start_command)
    assert allow_match is not None
    assert allow_match.group(1) == PRODUCTION_FORWARDED_ALLOW_IPS

    cidr_match = re.search(
        r'ADMIN_TRUSTED_PROXY_CIDRS\s*\n\s*value:\s*"([^"]+)"',
        render_yaml,
    )
    assert cidr_match is not None
    assert cidr_match.group(1) == PRODUCTION_FORWARDED_ALLOW_IPS


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextmanager
def _uvicorn_proxy_server(
    *,
    trusted_hosts: str,
    port: int,
) -> Generator[None, None, None]:
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
            "--proxy-headers",
            "--forwarded-allow-ips",
            trusted_hosts,
            "--log-level",
            "warning",
        ],
        cwd=REPO_ROOT,
        env={
            key: value
            for key, value in os.environ.items()
            if key != "DATABASE_URL"
        }
        | {
            "ADMIN_USERNAME": "operator",
            "ADMIN_PASSWORD_HASH": "$argon2id$v=19$m=65536,t=3,p=4$test",
            "ADMIN_SESSION_SECRET": "integration-test-secret-32chars-min",
            "BASE_URL": "http://127.0.0.1",
            "ADMIN_TRUSTED_PROXY_CIDRS": trusted_hosts,
            "ADMIN_LOGIN_RATE_LIMIT": "2",
        },
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 15
        with httpx.Client() as client:
            while time.monotonic() < deadline:
                try:
                    response = client.get(f"http://127.0.0.1:{port}/health", timeout=1.0)
                    if response.status_code == 200:
                        break
                except httpx.HTTPError:
                    time.sleep(0.1)
            else:
                raise RuntimeError("uvicorn did not become ready")
        yield
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.mark.integration
def test_uvicorn_proxy_configuration_resolves_sources_for_limiter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", "127.0.0.1/32")
    probe_app = FastAPI()

    @probe_app.post("/probe")
    async def probe(request: Request) -> dict[str, str]:
        settings = get_settings()
        return {
            "source": admin_auth.client_ip(request, settings),
            "peer": request.client.host if request.client else "",
        }

    wrapped = ProxyHeadersMiddleware(probe_app, trusted_hosts="127.0.0.1/32")
    client = TestClient(wrapped, client=("127.0.0.1", 50000))
    response = client.post(
        "/probe",
        headers={"X-Forwarded-For": "203.0.113.77, 127.0.0.1"},
    )
    payload = response.json()
    assert payload["source"] == "203.0.113.77"
    assert payload["peer"] == "203.0.113.77"

    port = _find_free_port()
    with _uvicorn_proxy_server(trusted_hosts="127.0.0.1/32", port=port):
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5.0) as client:
            health = client.get("/health")
            assert health.status_code == 200
            login_page = client.get("/admin/login")
            assert login_page.status_code in {200, 503}
