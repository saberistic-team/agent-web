"""Tests for trusted-proxy admin login client source resolution (#239)."""

from __future__ import annotations

import logging
import os
import re
import socket
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
    RESOLUTION_DIRECT_PEER,
    RESOLUTION_INVALID_FORWARDING_DATA,
    RESOLUTION_TRUSTED_CF_CONNECTING_IP,
    RESOLUTION_TRUSTED_FORWARDED,
    RESOLUTION_TRUSTED_X_FORWARDED_FOR,
    RESOLUTION_UNTRUSTED_HEADERS_IGNORED,
    normalize_ip_address,
    reset_client_source_telemetry,
    resolve_admin_login_client_source,
)
from app.config import get_settings

ROOT = Path(__file__).resolve().parents[1]
RENDER_PROXY = "10.0.0.1"
CLOUDFLARE_EDGE = "104.16.0.1"
REAL_CLIENT = "203.0.113.50"
ATTACKER_SPOOF = "198.51.100.99"
DIRECT_PEER = "198.51.100.10"


def _request(
    peer: str | None,
    headers: dict[str, str] | None = None,
) -> Request:
    scope: dict[str, Any] = {
        "type": "http",
        "headers": [
            (name.lower().encode("latin1"), value.encode("latin1"))
            for name, value in (headers or {}).items()
        ],
        "client": (peer, 12345) if peer is not None else None,
        "method": "POST",
        "path": "/admin/login",
    }
    return Request(scope)


@pytest.fixture
def trusted_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "ADMIN_TRUSTED_PROXY_CIDRS",
        "127.0.0.1/32,::1/128,10.0.0.0/8",
    )
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    reset_client_source_telemetry()


@pytest.mark.unit
def test_normalize_ipv4_and_ipv6_and_mapped() -> None:
    assert normalize_ip_address("203.0.113.1") == "203.0.113.1"
    assert normalize_ip_address("203.0.113.1:443") == "203.0.113.1"
    assert normalize_ip_address("2001:db8::1") == "2001:db8::1"
    assert normalize_ip_address("[2001:db8::1]:443") == "2001:db8::1"
    assert normalize_ip_address("::ffff:203.0.113.1") == "203.0.113.1"
    assert normalize_ip_address("") is None
    assert normalize_ip_address("not-an-ip") is None


@pytest.mark.unit
def test_direct_spoof_single_and_multi_xff_ignored(trusted_proxy_env: None) -> None:
    settings = get_settings()
    for header in (
        ATTACKER_SPOOF,
        f"{ATTACKER_SPOOF}, {REAL_CLIENT}",
    ):
        request = _request(
            DIRECT_PEER,
            headers={"X-Forwarded-For": header},
        )
        resolution = resolve_admin_login_client_source(request, settings)
        assert resolution.source == DIRECT_PEER
        assert resolution.path == RESOLUTION_UNTRUSTED_HEADERS_IGNORED


@pytest.mark.unit
def test_cloudflare_append_ignores_attacker_leftmost(trusted_proxy_env: None) -> None:
    settings = get_settings()
    request = _request(
        RENDER_PROXY,
        headers={
            "X-Forwarded-For": f"{ATTACKER_SPOOF}, {REAL_CLIENT}, {CLOUDFLARE_EDGE}",
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
    assert resolution.path == RESOLUTION_TRUSTED_X_FORWARDED_FOR


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client(trusted_proxy_env: None) -> None:
    settings = get_settings()
    request = _request(
        RENDER_PROXY,
        headers={"X-Forwarded-For": f"{REAL_CLIENT}, {CLOUDFLARE_EDGE}"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
    assert resolution.path == RESOLUTION_TRUSTED_X_FORWARDED_FOR


@pytest.mark.unit
def test_partial_trust_untrusted_intermediary_fails_closed(
    trusted_proxy_env: None,
) -> None:
    settings = get_settings()
    request = _request(
        DIRECT_PEER,
        headers={"X-Forwarded-For": f"{REAL_CLIENT}, {RENDER_PROXY}"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == DIRECT_PEER
    assert resolution.path == RESOLUTION_UNTRUSTED_HEADERS_IGNORED


@pytest.mark.unit
def test_direct_render_origin_ignores_cf_connecting_ip(
    trusted_proxy_env: None,
) -> None:
    settings = get_settings()
    request = _request(
        DIRECT_PEER,
        headers={
            "CF-Connecting-IP": REAL_CLIENT,
            "X-Forwarded-For": REAL_CLIENT,
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == DIRECT_PEER
    assert resolution.path == RESOLUTION_UNTRUSTED_HEADERS_IGNORED


@pytest.mark.unit
def test_cf_connecting_ip_used_when_cloudflare_hop_present(
    trusted_proxy_env: None,
) -> None:
    settings = get_settings()
    request = _request(
        RENDER_PROXY,
        headers={
            "CF-Connecting-IP": REAL_CLIENT,
            "X-Forwarded-For": CLOUDFLARE_EDGE,
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
    assert resolution.path == RESOLUTION_TRUSTED_CF_CONNECTING_IP


@pytest.mark.unit
def test_forwarded_header_precedence_after_xff_absent(
    trusted_proxy_env: None,
) -> None:
    settings = get_settings()
    request = _request(
        RENDER_PROXY,
        headers={
            "Forwarded": f'for="{REAL_CLIENT}", for="{CLOUDFLARE_EDGE}"',
            "CF-Connecting-IP": "203.0.113.77",
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
    assert resolution.path == RESOLUTION_TRUSTED_FORWARDED


@pytest.mark.unit
def test_xff_precedence_over_conflicting_forwarded_and_cf(
    trusted_proxy_env: None,
) -> None:
    settings = get_settings()
    request = _request(
        RENDER_PROXY,
        headers={
            "X-Forwarded-For": f"{REAL_CLIENT}, {CLOUDFLARE_EDGE}",
            "Forwarded": f'for="203.0.113.88"',
            "CF-Connecting-IP": "203.0.113.77",
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
    assert resolution.path == RESOLUTION_TRUSTED_X_FORWARDED_FOR


@pytest.mark.unit
def test_invalid_overlong_and_malformed_forwarding_data(
    trusted_proxy_env: None,
) -> None:
    settings = get_settings()
    overlong = ", ".join(f"10.0.0.{index}" for index in range(40))
    request = _request(RENDER_PROXY, headers={"X-Forwarded-For": overlong})
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.path == RESOLUTION_INVALID_FORWARDING_DATA

    malformed = _request(
        RENDER_PROXY,
        headers={"X-Forwarded-For": "not-an-ip, 10.0.0.2"},
    )
    bad = resolve_admin_login_client_source(malformed, settings)
    assert bad.path == RESOLUTION_INVALID_FORWARDING_DATA


@pytest.mark.unit
def test_missing_peer_uses_unknown(trusted_proxy_env: None) -> None:
    settings = get_settings()
    resolution = resolve_admin_login_client_source(_request(None), settings)
    assert resolution.source == "unknown"
    assert resolution.path == RESOLUTION_DIRECT_PEER


@pytest.mark.unit
def test_no_proxy_cidrs_uses_direct_peer_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    settings = get_settings()
    request = _request(
        RENDER_PROXY,
        headers={"X-Forwarded-For": REAL_CLIENT},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == RENDER_PROXY
    assert resolution.path == RESOLUTION_UNTRUSTED_HEADERS_IGNORED


@pytest.mark.unit
def test_untrusted_forwarding_attempt_emits_sampled_telemetry(
    trusted_proxy_env: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = get_settings()
    caplog.set_level(logging.INFO, logger="app.admin_client_source")
    reset_client_source_telemetry()
    request = _request(DIRECT_PEER, headers={"X-Forwarded-For": ATTACKER_SPOOF})
    resolve_admin_login_client_source(request, settings)
    with patch("app.admin_client_source.time.monotonic", return_value=1000.0):
        resolve_admin_login_client_source(request, settings)
    assert any(
        record.getMessage() == "Admin login source ignored untrusted forwarding headers"
        for record in caplog.records
    )
    assert "203.0.113" not in caplog.text
    assert "X-Forwarded-For" not in caplog.text



@pytest.mark.unit
def test_render_yaml_proxy_trust_configuration() -> None:
    render_yaml = (ROOT / "render.yaml").read_text(encoding="utf-8")
    assert "--forwarded-allow-ips=''" in render_yaml
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in render_yaml
    assert "UVICORN_FORWARDED_ALLOW_IPS" in render_yaml
    assert 'value: ""' in render_yaml


@pytest.mark.unit
def test_admin_auth_docs_describe_trusted_hop_model() -> None:
    docs = (ROOT / "docs" / "ADMIN_AUTH.md").read_text(encoding="utf-8")
    assert "Right-to-left parse" in docs
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in docs
    assert "--forwarded-allow-ips=''" in docs
    assert "ADMIN_TRUST_PROXY_HEADERS" in docs


@pytest.mark.unit
@pytest.mark.integration
def test_health_reports_admin_source_trust_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", "10.0.0.0/8")
    monkeypatch.setenv("UVICORN_FORWARDED_ALLOW_IPS", "")
    client = TestClient(app)
    payload = client.get("/health").json()
    trust = payload.get("admin_login_source_trust")
    assert trust is not None
    assert trust["proxy_boundary_configured"] is True
    assert trust["uvicorn_forwarded_allow_ips_disabled"] is True


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.mark.integration
def test_uvicorn_deploy_proxy_configuration_resolves_trusted_xff() -> None:
    """Exercise uvicorn with production forwarded-allow-ips disabled."""
    port = _free_port()
    env = os.environ.copy()
    env.update(
        {
            "DATABASE_URL": "",
            "ADMIN_TRUSTED_PROXY_CIDRS": "127.0.0.1/32,10.0.0.0/8",
            "UVICORN_FORWARDED_ALLOW_IPS": "",
            "BASE_URL": "http://127.0.0.1",
        }
    )
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
            "",
        ],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.time() + 20
        last_error: Exception | None = None
        while time.time() < deadline:
            if proc.poll() is not None:
                stdout, stderr = proc.communicate(timeout=1)
                raise AssertionError(
                    f"uvicorn exited early ({proc.returncode}): {stderr or stdout}"
                )
            try:
                response = httpx.get(f"http://127.0.0.1:{port}/health", timeout=1.0)
                if response.status_code == 200:
                    break
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                time.sleep(0.2)
        else:
            proc.terminate()
            stdout, stderr = proc.communicate(timeout=2)
            raise AssertionError(
                f"uvicorn failed to start: {last_error}\nstderr={stderr}\nstdout={stdout}"
            )

        scope_request = _request(
            "127.0.0.1",
            headers={"X-Forwarded-For": f"{ATTACKER_SPOOF}, {REAL_CLIENT}"},
        )
        with patch.dict(
            os.environ,
            {
                "ADMIN_TRUSTED_PROXY_CIDRS": "127.0.0.1/32,10.0.0.0/8",
                "ADMIN_TRUST_PROXY_HEADERS": "",
            },
            clear=False,
        ):
            resolution = resolve_admin_login_client_source(scope_request, get_settings())
        assert resolution.source == REAL_CLIENT
        assert resolution.path == RESOLUTION_TRUSTED_X_FORWARDED_FOR

        health = httpx.get(f"http://127.0.0.1:{port}/health", timeout=2.0).json()
        assert health["admin_login_source_trust"]["uvicorn_forwarded_allow_ips_disabled"]
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@pytest.mark.unit
def test_limiter_keys_and_logs_contain_no_raw_forwarding_data(
    trusted_proxy_env: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = get_settings()
    caplog.set_level(logging.INFO, logger="app.admin_auth")
    request = _request(
        DIRECT_PEER,
        headers={
            "X-Forwarded-For": f"{ATTACKER_SPOOF}, {REAL_CLIENT}",
            "CF-Connecting-IP": REAL_CLIENT,
        },
    )
    source = admin_auth.client_ip(request, settings)
    key = admin_auth.build_source_rate_limit_key(source)
    assert ATTACKER_SPOOF not in key
    assert REAL_CLIENT not in key
    assert re.fullmatch(r"[0-9a-f]{64}", key)
    with patch("app.admin_auth.db.db_connection", side_effect=Exception("down")):
        admin_auth.try_admit_login_attempt(request, settings, username="ghost")
    joined = "\n".join(record.message for record in caplog.records)
    assert ATTACKER_SPOOF not in joined
    assert REAL_CLIENT not in joined
    assert "X-Forwarded-For" not in joined
