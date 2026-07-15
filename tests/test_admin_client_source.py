"""Tests for trusted-proxy admin login client source resolution (#239)."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from fastapi import Request

from app import admin_auth
from app.admin_client_source import (
    SourceResolutionPath,
    normalize_client_source,
    reset_client_source_telemetry,
    resolve_admin_login_client_source,
)
from app.config import Settings, get_settings

REPO_ROOT = Path(__file__).resolve().parents[1]
RENDER_PROXY = "10.0.0.0/8"
CF_EDGE = "172.64.0.0/13"


@pytest.fixture(autouse=True)
def _reset_telemetry() -> None:
    reset_client_source_telemetry()


def _request(
    host: str,
    *,
    headers: dict[str, str] | None = None,
) -> Request:
    header_list = [
        (name.lower().encode("latin-1"), value.encode("latin-1"))
        for name, value in (headers or {}).items()
    ]
    scope: dict[str, Any] = {
        "type": "http",
        "headers": header_list,
        "client": (host, 12345),
        "method": "POST",
        "path": "/admin/login",
    }
    return Request(scope)


def _trusted_settings(
    monkeypatch: pytest.MonkeyPatch,
    *,
    proxy_cidrs: str = RENDER_PROXY,
    edge_cidrs: str = CF_EDGE,
) -> Settings:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", proxy_cidrs)
    monkeypatch.setenv("ADMIN_TRUSTED_EDGE_CIDRS", edge_cidrs)
    return get_settings()


@pytest.mark.unit
def test_normalize_client_source_formats() -> None:
    assert normalize_client_source("203.0.113.1") == "203.0.113.1"
    assert normalize_client_source("203.0.113.1:8080") == "203.0.113.1"
    assert normalize_client_source("[2001:db8::1]:443") == "2001:db8::1"
    assert normalize_client_source("::ffff:203.0.113.1") == "203.0.113.1"
    assert normalize_client_source("  203.0.113.2  ") == "203.0.113.2"
    assert normalize_client_source("") is None
    assert normalize_client_source("not-an-ip") is None


@pytest.mark.unit
def test_direct_spoof_single_and_multi_hop_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _trusted_settings(monkeypatch)
    request = _request(
        "203.0.113.10",
        headers={
            "X-Forwarded-For": "198.51.100.99",
            "CF-Connecting-IP": "198.51.100.99",
            "Forwarded": 'for="198.51.100.99"',
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.10"
    assert resolution.path is SourceResolutionPath.UNTRUSTED_PEER

    request_multi = _request(
        "203.0.113.10",
        headers={"X-Forwarded-For": "198.51.100.1, 198.51.100.2, 198.51.100.3"},
    )
    resolution_multi = resolve_admin_login_client_source(request_multi, settings)
    assert resolution_multi.source == "203.0.113.10"
    assert resolution_multi.path is SourceResolutionPath.UNTRUSTED_PEER


@pytest.mark.unit
def test_cloudflare_append_ignores_attacker_leftmost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _trusted_settings(monkeypatch)
    request = _request(
        "10.0.0.5",
        headers={"X-Forwarded-For": "198.51.100.99, 203.0.113.77"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.77"
    assert resolution.path is SourceResolutionPath.FORWARDED_CHAIN


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _trusted_settings(monkeypatch)
    request = _request(
        "10.0.0.5",
        headers={"X-Forwarded-For": "203.0.113.50, 10.0.0.2, 172.64.12.34"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.50"
    assert resolution.path is SourceResolutionPath.FORWARDED_CHAIN


@pytest.mark.unit
def test_partial_trust_fails_closed_to_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _trusted_settings(monkeypatch)
    request = _request(
        "10.0.0.5",
        headers={"X-Forwarded-For": "203.0.113.60"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "10.0.0.5"
    assert resolution.path is SourceResolutionPath.TRUSTED_PEER_FALLBACK


@pytest.mark.unit
def test_direct_render_origin_ignores_cloudflare_vendor_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _trusted_settings(monkeypatch)
    request = _request(
        "203.0.113.10",
        headers={
            "CF-Connecting-IP": "198.51.100.55",
            "X-Forwarded-For": "198.51.100.55",
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.10"
    assert resolution.path is SourceResolutionPath.UNTRUSTED_PEER


@pytest.mark.unit
def test_header_precedence_xff_before_cf_and_forwarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _trusted_settings(monkeypatch)
    request = _request(
        "10.0.0.5",
        headers={
            "X-Forwarded-For": "203.0.113.80, 203.0.113.81",
            "Forwarded": 'for="203.0.113.70"',
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.81"
    assert resolution.path is SourceResolutionPath.FORWARDED_CHAIN

    request_forwarded_only = _request(
        "10.0.0.5",
        headers={"Forwarded": 'for="203.0.113.70", for="203.0.113.71"'},
    )
    resolution_forwarded = resolve_admin_login_client_source(request_forwarded_only, settings)
    assert resolution_forwarded.source == "203.0.113.71"
    assert resolution_forwarded.path is SourceResolutionPath.FORWARDED_RFC7239


@pytest.mark.unit
def test_cf_connecting_ip_when_consistent_with_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _trusted_settings(monkeypatch)
    request = _request(
        "10.0.0.5",
        headers={
            "X-Forwarded-For": "203.0.113.90, 203.0.113.91",
            "CF-Connecting-IP": "203.0.113.91",
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.91"
    assert resolution.path is SourceResolutionPath.FORWARDED_CHAIN


@pytest.mark.unit
def test_cf_connecting_ip_mismatch_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _trusted_settings(monkeypatch)
    request = _request(
        "10.0.0.5",
        headers={
            "X-Forwarded-For": "203.0.113.80, 203.0.113.81",
            "CF-Connecting-IP": "203.0.113.99",
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "10.0.0.5"
    assert resolution.path is SourceResolutionPath.TRUSTED_PEER_FALLBACK
    assert resolution.invalid_forwarding is True


@pytest.mark.unit
def test_malformed_and_overlong_forwarding_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _trusted_settings(monkeypatch)
    request_invalid = _request(
        "10.0.0.5",
        headers={"X-Forwarded-For": "not-an-ip, 203.0.113.1"},
    )
    resolution_invalid = resolve_admin_login_client_source(request_invalid, settings)
    assert resolution_invalid.source == "10.0.0.5"
    assert resolution_invalid.path is SourceResolutionPath.TRUSTED_PEER_FALLBACK

    long_chain = ", ".join(f"203.0.113.{index}" for index in range(40))
    request_long = _request("10.0.0.5", headers={"X-Forwarded-For": long_chain})
    resolution_long = resolve_admin_login_client_source(request_long, settings)
    assert resolution_long.source == "10.0.0.5"
    assert resolution_long.invalid_forwarding is True


@pytest.mark.unit
@pytest.mark.integration
def test_rotating_spoofed_headers_share_one_limiter_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_admin_auth import (
        FakeRateLimitStore,
        TEST_HASH,
        TEST_SECRET,
        TEST_USERNAME,
        _login,
        mock_db_connection,
        shared_rate_limiter,
    )

    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "3")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", RENDER_PROXY)
    monkeypatch.setenv("ADMIN_TRUSTED_EDGE_CIDRS", CF_EDGE)
    store = FakeRateLimitStore()

    with shared_rate_limiter(store):
        with mock_db_connection():
            for index in range(3):
                headers = {"X-Forwarded-For": f"198.51.100.{index}, 203.0.113.44"}
                response = _login(
                    username="ghost",
                    password="wrong",
                    headers=headers,
                )
                assert response.status_code == 401

            blocked = _login(
                username="ghost",
                password="wrong",
                headers={"X-Forwarded-For": "198.51.100.99, 203.0.113.44"},
            )
            assert blocked.status_code == 429

    assert len(store.rows) == 1
    assert admin_auth.build_source_rate_limit_key("testclient") in store.rows


@pytest.mark.unit
def test_telemetry_and_logs_exclude_raw_forwarding_data(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    caplog.set_level(logging.INFO, logger="app.admin_client_source")
    settings = _trusted_settings(monkeypatch)
    request = _request(
        "203.0.113.10",
        headers={"X-Forwarded-For": "198.51.100.99, 198.51.100.100"},
    )
    with patch("app.admin_client_source._TELEMETRY_SAMPLE_INTERVAL", 0.0):
        resolve_admin_login_client_source(request, settings)

    messages = " ".join(record.getMessage() for record in caplog.records)
    extras = " ".join(
        str(value)
        for record in caplog.records
        for value in getattr(record, "__dict__", {}).values()
    )
    combined = f"{messages} {extras}"
    assert "198.51.100.99" not in combined
    assert "198.51.100.100" not in combined


@pytest.mark.unit
def test_health_reports_proxy_trust_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.main import health

    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    monkeypatch.delenv("ADMIN_TRUSTED_EDGE_CIDRS", raising=False)
    payload = health()
    assert payload["admin_client_source_trust"] == {
        "proxy_cidrs_configured": False,
        "edge_cidrs_configured": False,
    }

    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", RENDER_PROXY)
    monkeypatch.setenv("ADMIN_TRUSTED_EDGE_CIDRS", CF_EDGE)
    payload_configured = health()
    assert payload_configured["admin_client_source_trust"]["proxy_cidrs_configured"] is True
    assert payload_configured["admin_client_source_trust"]["edge_cidrs_configured"] is True


@pytest.mark.unit
def test_render_yaml_proxy_trust_settings_are_consistent() -> None:
    render_yaml = (REPO_ROOT / "render.yaml").read_text(encoding="utf-8")
    assert "--proxy-headers" in render_yaml
    assert "--forwarded-allow-ips=" in render_yaml
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in render_yaml
    assert "ADMIN_TRUSTED_EDGE_CIDRS" in render_yaml
    assert "ADMIN_TRUST_PROXY_HEADERS" not in render_yaml


@pytest.mark.unit
def test_admin_auth_docs_match_proxy_trust_model() -> None:
    docs = (REPO_ROOT / "docs" / "ADMIN_AUTH.md").read_text(encoding="utf-8")
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in docs
    assert "ADMIN_TRUSTED_EDGE_CIDRS" in docs
    assert "right-to-left" in docs
    assert "ADMIN_TRUST_PROXY_HEADERS" not in docs


@pytest.mark.integration
def test_uvicorn_proxy_configuration_resolves_trusted_chain() -> None:
    env = os.environ.copy()
    env.update(
        {
            "DATABASE_URL": "",
            "ADMIN_USERNAME": "",
            "ADMIN_PASSWORD_HASH": "",
            "ADMIN_SESSION_SECRET": "",
            "ADMIN_TRUSTED_PROXY_CIDRS": RENDER_PROXY,
            "ADMIN_TRUSTED_EDGE_CIDRS": CF_EDGE,
            "BASE_URL": "http://127.0.0.1:8765",
        }
    )
    port = 28765
    command = [
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
        "127.0.0.1",
    ]
    process = subprocess.Popen(
        command,
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                response = httpx.get(f"http://127.0.0.1:{port}/health", timeout=1.0)
                if response.status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.2)
        else:
            raise AssertionError("uvicorn did not become ready")

        health_payload = response.json()
        assert health_payload["admin_client_source_trust"]["proxy_cidrs_configured"] is True

        from app.admin_client_source import resolve_admin_login_client_source
        from app.config import get_settings

        os.environ["ADMIN_TRUSTED_PROXY_CIDRS"] = RENDER_PROXY
        os.environ["ADMIN_TRUSTED_EDGE_CIDRS"] = CF_EDGE
        settings = get_settings()
        scope_request = _request(
            "10.0.0.1",
            headers={"X-Forwarded-For": "198.51.100.5, 203.0.113.42"},
        )
        resolution = resolve_admin_login_client_source(scope_request, settings)
        assert resolution.source == "203.0.113.42"
        assert resolution.path is SourceResolutionPath.FORWARDED_CHAIN
    finally:
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=10)
