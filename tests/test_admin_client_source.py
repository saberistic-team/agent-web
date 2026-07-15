"""Tests for trusted-proxy admin login client source resolution (#239)."""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from typing import Generator

import httpx
import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from app import admin_auth
from app.admin_client_source import (
    ClientSourceResolutionPath,
    emit_client_source_resolution_telemetry,
    normalize_client_source,
    reset_client_source_telemetry_for_tests,
    resolve_admin_login_client_source,
)
from app.config import get_settings
from app.main import app
from tests.test_admin_auth import (
    FakeRateLimitStore,
    TEST_HASH,
    TEST_SECRET,
    TEST_USERNAME,
    _parse_login_form,
    mock_db_connection,
    shared_rate_limiter,
)

RENDER_LB = "10.0.0.5"
REAL_CLIENT = "203.0.113.50"
SPOOFED_CLIENT = "203.0.113.99"
CF_EDGE = "104.16.132.229"


@pytest.fixture
def rate_limit_store() -> FakeRateLimitStore:
    return FakeRateLimitStore()


@pytest.fixture(autouse=True)
def admin_client_source_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "5")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_WINDOW_SECONDS", "900")
    monkeypatch.setenv("ADMIN_LOGIN_LOCKOUT_SECONDS", "900")
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    monkeypatch.delenv("ADMIN_LOGIN_TRUST_FORWARDED_HEADERS", raising=False)
    admin_auth.reset_login_rate_limiter()
    reset_client_source_telemetry_for_tests()


def _trusted_client() -> TestClient:
    return TestClient(app, follow_redirects=False, client=(RENDER_LB, 50000))


def _request_with_client(host: str, headers: list[tuple[bytes, bytes]] | None = None) -> Request:
    scope = {
        "type": "http",
        "headers": headers or [],
        "client": (host, 12345),
        "method": "POST",
        "path": "/admin/login",
    }
    return Request(scope)


def _settings_with_trust(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_LOGIN_TRUST_FORWARDED_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    reset_client_source_telemetry_for_tests()
    return get_settings()


@pytest.fixture(autouse=True)
def _reset_telemetry() -> Generator[None, None, None]:
    reset_client_source_telemetry_for_tests()
    yield
    reset_client_source_telemetry_for_tests()


@pytest.mark.unit
def test_direct_spoof_single_and_multi_hop_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    for header_value in (SPOOFED_CLIENT, f"{SPOOFED_CLIENT}, {REAL_CLIENT}"):
        request = _request_with_client(
            "198.51.100.10",
            [(b"x-forwarded-for", header_value.encode())],
        )
        resolution = resolve_admin_login_client_source(request, settings)
        assert resolution.source == "198.51.100.10"
        assert resolution.path is ClientSourceResolutionPath.DIRECT_PEER


@pytest.mark.unit
def test_cloudflare_append_ignores_spoofed_leftmost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_trust(monkeypatch)
    request = _request_with_client(
        RENDER_LB,
        [(b"x-forwarded-for", f"{SPOOFED_CLIENT}, {REAL_CLIENT}".encode())],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
    assert resolution.path is ClientSourceResolutionPath.FORWARDED_TRUSTED


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_trust(monkeypatch)
    request = _request_with_client(
        RENDER_LB,
        [(b"x-forwarded-for", f"{REAL_CLIENT}, {CF_EDGE}".encode())],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
    assert resolution.path is ClientSourceResolutionPath.FORWARDED_TRUSTED


@pytest.mark.unit
def test_partial_trust_untrusted_intermediary_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_trust(monkeypatch)
    request = _request_with_client(
        "198.51.100.10",
        [(b"x-forwarded-for", f"{REAL_CLIENT}, {RENDER_LB}".encode())],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "198.51.100.10"
    assert resolution.path is ClientSourceResolutionPath.FORWARDED_UNTRUSTED_PEER


@pytest.mark.unit
def test_direct_render_origin_ignores_cf_connecting_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_trust(monkeypatch)
    request = _request_with_client(
        RENDER_LB,
        [
            (b"cf-connecting-ip", REAL_CLIENT.encode()),
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == RENDER_LB
    assert resolution.path is ClientSourceResolutionPath.CF_CONNECTING_IP_REJECTED


@pytest.mark.unit
def test_header_precedence_prefers_x_forwarded_for_over_forwarded_and_cf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_trust(monkeypatch)
    request = _request_with_client(
        RENDER_LB,
        [
            (b"x-forwarded-for", REAL_CLIENT.encode()),
            (b"forwarded", b'for="203.0.113.77"'),
            (b"cf-connecting-ip", b"203.0.113.88"),
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT


@pytest.mark.unit
def test_conflicting_xff_and_forwarded_uses_xff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_trust(monkeypatch)
    request = _request_with_client(
        RENDER_LB,
        [
            (b"x-forwarded-for", REAL_CLIENT.encode()),
            (b"forwarded", b'for="203.0.113.77"'),
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
    assert resolution.path is ClientSourceResolutionPath.FORWARDED_HEADER_CONFLICT


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("203.0.113.1", "203.0.113.1"),
        ("203.0.113.1:8080", "203.0.113.1"),
        ("2001:db8::1", "2001:db8::1"),
        ("[2001:db8::1]:443", "2001:db8::1"),
        ("::ffff:203.0.113.1", "203.0.113.1"),
        ("  203.0.113.1  ", "203.0.113.1"),
        ("not-an-ip", None),
        ("", None),
    ],
)
def test_normalize_client_source_formats(raw: str, expected: str | None) -> None:
    assert normalize_client_source(raw) == expected


@pytest.mark.unit
def test_malformed_and_overlong_forwarding_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings_with_trust(monkeypatch)
    monkeypatch.setenv("ADMIN_LOGIN_MAX_FORWARDED_CHAIN_LENGTH", "2")

    malformed = _request_with_client(
        RENDER_LB,
        [(b"x-forwarded-for", b"totally-invalid-ip")],
    )
    assert resolve_admin_login_client_source(malformed, get_settings()).source == RENDER_LB

    overlong = _request_with_client(
        RENDER_LB,
        [(b"x-forwarded-for", b"203.0.113.1, 203.0.113.2, 203.0.113.3")],
    )
    resolution = resolve_admin_login_client_source(overlong, get_settings())
    assert resolution.source == RENDER_LB
    assert resolution.path is ClientSourceResolutionPath.FORWARDED_TOO_LONG


@pytest.mark.unit
@pytest.mark.integration
def test_rotating_spoofed_headers_do_not_create_new_limiter_rows(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "3")
    monkeypatch.setenv("ADMIN_LOGIN_TRUST_FORWARDED_HEADERS", "true")
    http_client = _trusted_client()
    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            for index in range(4):
                form = http_client.get("/admin/login")
                csrf_token, cookies = _parse_login_form(form)
                response = http_client.post(
                    "/admin/login",
                    data={
                        "username": f"user-{index}",
                        "password": "wrong",
                        "csrf_token": csrf_token,
                    },
                    cookies=cookies,
                    headers={
                        "X-Forwarded-For": f"203.0.113.{index}, {REAL_CLIENT}",
                    },
                )
                if index < 3:
                    assert response.status_code == 401
                else:
                    assert response.status_code == 429
    assert len(rate_limit_store.rows) == 1
    source_key = admin_auth.build_source_rate_limit_key(REAL_CLIENT)
    assert source_key in rate_limit_store.rows


@pytest.mark.unit
@pytest.mark.integration
def test_trusted_peer_rate_limit_uses_resolved_forwarded_client(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    monkeypatch.setenv("ADMIN_LOGIN_TRUST_FORWARDED_HEADERS", "true")
    http_client = _trusted_client()
    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            headers_a = {"X-Forwarded-For": "203.0.113.77"}
            headers_b = {"X-Forwarded-For": "203.0.113.88"}
            for headers in (headers_a, headers_a):
                form = http_client.get("/admin/login")
                csrf_token, cookies = _parse_login_form(form)
                assert http_client.post(
                    "/admin/login",
                    data={
                        "username": "ghost",
                        "password": "wrong",
                        "csrf_token": csrf_token,
                    },
                    cookies=cookies,
                    headers=headers,
                ).status_code == 401

            form = http_client.get("/admin/login")
            csrf_token, cookies = _parse_login_form(form)
            assert http_client.post(
                "/admin/login",
                data={
                    "username": "ghost",
                    "password": "wrong",
                    "csrf_token": csrf_token,
                },
                cookies=cookies,
                headers=headers_a,
            ).status_code == 429

            form = http_client.get("/admin/login")
            csrf_token, cookies = _parse_login_form(form)
            assert http_client.post(
                "/admin/login",
                data={
                    "username": "ghost",
                    "password": "wrong",
                    "csrf_token": csrf_token,
                },
                cookies=cookies,
                headers=headers_b,
            ).status_code == 401


@pytest.mark.unit
def test_telemetry_contains_resolution_path_not_raw_addresses(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    emit_client_source_resolution_telemetry(ClientSourceResolutionPath.FORWARDED_UNTRUSTED_PEER)
    messages = [record.getMessage() for record in caplog.records]
    assert "Admin login forwarding header rejected" in messages
    serialized = str(caplog.records)
    assert REAL_CLIENT not in serialized
    assert SPOOFED_CLIENT not in serialized
    assert "x-forwarded-for" not in serialized.lower()


@pytest.mark.unit
def test_limiter_rows_store_digests_not_raw_source(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "1")
    monkeypatch.setenv("ADMIN_LOGIN_TRUST_FORWARDED_HEADERS", "true")
    http_client = _trusted_client()
    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            form = http_client.get("/admin/login")
            csrf_token, cookies = _parse_login_form(form)
            http_client.post(
                "/admin/login",
                data={
                    "username": TEST_USERNAME,
                    "password": "wrong-password",
                    "csrf_token": csrf_token,
                },
                cookies=cookies,
                headers={"X-Forwarded-For": REAL_CLIENT},
            )
    for key in rate_limit_store.rows:
        assert REAL_CLIENT not in key
        assert len(key) == 64


@contextmanager
def _free_port() -> Generator[int, None, None]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    yield port


@pytest.mark.integration
def test_uvicorn_start_command_serves_health_with_forwarded_allow_ips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the render.yaml uvicorn proxy boundary against a live process."""
    with _free_port() as port:
        uvicorn_env = {
            key: value
            for key, value in os.environ.items()
            if key not in {"DATABASE_URL", "ADMIN_TRUST_PROXY_HEADERS", "ADMIN_LOGIN_TRUST_FORWARDED_HEADERS"}
        }
        uvicorn_env["BASE_URL"] = f"http://127.0.0.1:{port}"
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
                "--forwarded-allow-ips=127.0.0.1",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=uvicorn_env,
        )
        try:
            deadline = time.time() + 20
            last_error: Exception | None = None
            response = None
            while time.time() < deadline:
                try:
                    response = httpx.get(f"http://127.0.0.1:{port}/health", timeout=1.0)
                    if response.status_code == 200:
                        break
                except httpx.HTTPError as exc:
                    last_error = exc
                    time.sleep(0.2)
            else:
                stderr = proc.stderr.read().decode() if proc.stderr is not None else ""
                raise AssertionError(f"uvicorn health check failed: {last_error}; stderr={stderr}")
            assert response is not None
            payload = response.json()
            assert payload.get("status") == "ok"
        finally:
            proc.terminate()
            proc.wait(timeout=10)
            if proc.returncode not in (0, -15, None):
                pytest.fail(f"uvicorn exited with code {proc.returncode}")
