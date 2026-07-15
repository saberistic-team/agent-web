"""Tests for trusted-proxy admin login client source resolution."""

from __future__ import annotations

import json
import os
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
    ClientSourceResolution,
    normalize_ip_address,
    reset_client_source_telemetry,
    resolve_admin_login_client_source,
)
from app.config import get_settings

REPO_ROOT = Path(__file__).resolve().parent.parent
RENDER_PROXY = "10.0.0.1"
CLOUDFLARE_PROXY = "173.245.48.1"
CLIENT_IPV4 = "203.0.113.50"
CLIENT_IPV6 = "2001:db8::50"
SPOOFED_IPV4 = "203.0.113.99"
UNTRUSTED_PEER = "198.51.100.10"

TEST_TRUSTED_CIDRS = "10.0.0.0/8,127.0.0.1"
TEST_CF_CIDRS = "173.245.48.0/20"


@pytest.fixture(autouse=True)
def reset_telemetry() -> None:
    reset_client_source_telemetry()


@pytest.fixture
def proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", TEST_TRUSTED_CIDRS)
    monkeypatch.setenv("ADMIN_CLOUDFLARE_TRUST_ENABLED", "true")
    monkeypatch.setenv("ADMIN_CLOUDFLARE_PROXY_CIDRS", TEST_CF_CIDRS)
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)


def _request(
    *,
    peer: str,
    headers: dict[str, str] | None = None,
    transport_peer: str | None = None,
) -> Request:
    header_list = [
        (key.lower().encode("ascii"), value.encode("ascii"))
        for key, value in (headers or {}).items()
    ]
    scope: dict[str, Any] = {
        "type": "http",
        "headers": header_list,
        "client": (peer, 12345),
        "method": "POST",
        "path": "/admin/login",
        "state": {},
    }
    if transport_peer is not None:
        scope["state"]["transport_peer"] = (transport_peer, 12345)
    return Request(scope)


def _resolve(request: Request) -> ClientSourceResolution:
    return resolve_admin_login_client_source(request, get_settings())


@pytest.mark.unit
def test_normalize_ip_address_handles_none() -> None:
    assert normalize_ip_address(None) is None


@pytest.mark.unit
def test_parse_networks_skips_invalid_cidrs() -> None:
    from app.admin_client_source import parse_networks

    networks = parse_networks(["", "not-a-cidr", "10.0.0.0/8"])
    assert len(networks) == 1


@pytest.mark.unit
def test_ip_in_networks_rejects_invalid_ip() -> None:
    from app.admin_client_source import ip_in_networks, parse_networks

    assert ip_in_networks("not-an-ip", parse_networks(["10.0.0.0/8"])) is False


@pytest.mark.unit
def test_legacy_admin_trust_proxy_headers_uses_render_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    request = _request(
        peer=RENDER_PROXY,
        headers={"X-Forwarded-For": f"{CLIENT_IPV4}, {RENDER_PROXY}"},
        transport_peer=RENDER_PROXY,
    )
    result = resolve_admin_login_client_source(request, get_settings())
    assert result.address == CLIENT_IPV4


@pytest.mark.unit
def test_malformed_forwarded_header_falls_back(proxy_env: None) -> None:
    request = _request(
        peer=RENDER_PROXY,
        headers={"Forwarded": "proto=https"},
        transport_peer=RENDER_PROXY,
    )
    result = _resolve(request)
    assert result.path == "malformed_forwarding"


@pytest.mark.unit
def test_malformed_cf_connecting_ip_falls_back(proxy_env: None) -> None:
    request = _request(
        peer=RENDER_PROXY,
        headers={"CF-Connecting-IP": "not-an-ip"},
        transport_peer=RENDER_PROXY,
    )
    result = _resolve(request)
    assert result.path == "malformed_forwarding"


@pytest.mark.unit
def test_trusted_chain_of_only_trusted_hops_falls_back_to_peer(proxy_env: None) -> None:
    request = _request(
        peer=RENDER_PROXY,
        headers={"X-Forwarded-For": f"{RENDER_PROXY}, {CLOUDFLARE_PROXY}"},
        transport_peer=RENDER_PROXY,
    )
    result = _resolve(request)
    assert result.address == RENDER_PROXY
    assert result.path == "trusted_peer_only_trusted_hops"


@pytest.mark.unit
def test_trusted_proxy_without_forwarding_headers_uses_peer(proxy_env: None) -> None:
    request = _request(peer=RENDER_PROXY, transport_peer=RENDER_PROXY)
    result = _resolve(request)
    assert result.address == RENDER_PROXY
    assert result.path == "trusted_proxy_peer"


@pytest.mark.unit
def test_proxy_trust_health_summary_honors_legacy_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.admin_client_source import proxy_trust_health_summary

    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    summary = proxy_trust_health_summary(get_settings())
    assert summary["trusted_proxy_cidrs_configured"] is True


@pytest.mark.unit
def test_missing_peer_with_forwarding_headers_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    scope = {
        "type": "http",
        "headers": [(b"x-forwarded-for", b"203.0.113.1")],
        "client": None,
        "method": "POST",
        "path": "/admin/login",
        "state": {},
    }
    result = resolve_admin_login_client_source(Request(scope), get_settings())
    assert result.address == "unknown"
    assert result.path == "missing_peer"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("203.0.113.1", "203.0.113.1"),
        (" 203.0.113.1 ", "203.0.113.1"),
        ("203.0.113.1:443", "203.0.113.1"),
        ("[2001:db8::1]", "2001:db8::1"),
        ("[2001:db8::1]:443", "2001:db8::1"),
        ("::ffff:203.0.113.1", "203.0.113.1"),
        ("not-an-ip", None),
        ("", None),
    ],
)
def test_normalize_ip_address(raw: str, expected: str | None) -> None:
    assert normalize_ip_address(raw) == expected


@pytest.mark.unit
def test_direct_spoof_single_value_xff_ignored(proxy_env: None) -> None:
    request = _request(
        peer=UNTRUSTED_PEER,
        headers={"X-Forwarded-For": SPOOFED_IPV4},
        transport_peer=UNTRUSTED_PEER,
    )
    result = _resolve(request)
    assert result.address == UNTRUSTED_PEER
    assert result.path == "direct_peer"
    assert result.ignored_untrusted_forwarding is True


@pytest.mark.unit
def test_direct_spoof_multi_value_xff_ignored(proxy_env: None) -> None:
    request = _request(
        peer=UNTRUSTED_PEER,
        headers={"X-Forwarded-For": f"{SPOOFED_IPV4}, {CLIENT_IPV4}"},
        transport_peer=UNTRUSTED_PEER,
    )
    result = _resolve(request)
    assert result.address == UNTRUSTED_PEER
    assert result.path == "direct_peer"


@pytest.mark.unit
def test_cloudflare_append_ignores_attacker_leftmost(proxy_env: None) -> None:
    request = _request(
        peer=RENDER_PROXY,
        headers={"X-Forwarded-For": f"{SPOOFED_IPV4}, {CLIENT_IPV4}"},
        transport_peer=RENDER_PROXY,
    )
    result = _resolve(request)
    assert result.address == CLIENT_IPV4
    assert result.path == "xff_trusted_chain"


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client(proxy_env: None) -> None:
    request = _request(
        peer=RENDER_PROXY,
        headers={"X-Forwarded-For": f"{CLIENT_IPV4}, {CLOUDFLARE_PROXY}"},
        transport_peer=RENDER_PROXY,
    )
    result = _resolve(request)
    assert result.address == CLIENT_IPV4
    assert result.path == "xff_trusted_chain"


@pytest.mark.unit
def test_partial_trust_untrusted_intermediary_fails_closed(proxy_env: None) -> None:
    request = _request(
        peer=UNTRUSTED_PEER,
        headers={"X-Forwarded-For": f"{CLIENT_IPV4}, {RENDER_PROXY}"},
        transport_peer=UNTRUSTED_PEER,
    )
    result = _resolve(request)
    assert result.address == UNTRUSTED_PEER
    assert result.path == "direct_peer"


@pytest.mark.unit
def test_direct_render_origin_ignores_spoofed_cf_connecting_ip(proxy_env: None) -> None:
    request = _request(
        peer=RENDER_PROXY,
        headers={
            "X-Forwarded-For": SPOOFED_IPV4,
            "CF-Connecting-IP": CLIENT_IPV4,
        },
        transport_peer=RENDER_PROXY,
    )
    result = _resolve(request)
    assert result.address == SPOOFED_IPV4
    assert result.path == "xff_trusted_chain"


@pytest.mark.unit
def test_cf_connecting_ip_used_after_verified_cloudflare_hop(proxy_env: None) -> None:
    request = _request(
        peer=RENDER_PROXY,
        headers={
            "X-Forwarded-For": f"{CLIENT_IPV4}, {CLOUDFLARE_PROXY}",
            "CF-Connecting-IP": CLIENT_IPV4,
        },
        transport_peer=RENDER_PROXY,
    )
    result = _resolve(request)
    assert result.address == CLIENT_IPV4
    assert result.path == "cf_connecting_ip"


@pytest.mark.unit
def test_forwarded_header_precedence_after_xff_absent(proxy_env: None) -> None:
    request = _request(
        peer=RENDER_PROXY,
        headers={"Forwarded": f'for="{CLIENT_IPV4}";proto=https, for={CLOUDFLARE_PROXY}'},
        transport_peer=RENDER_PROXY,
    )
    result = _resolve(request)
    assert result.address == CLIENT_IPV4
    assert result.path == "forwarded_trusted_chain"


@pytest.mark.unit
def test_xff_precedence_over_forwarded_when_both_present(proxy_env: None) -> None:
    request = _request(
        peer=RENDER_PROXY,
        headers={
            "X-Forwarded-For": f"{CLIENT_IPV4}, {CLOUDFLARE_PROXY}",
            "Forwarded": f"for={SPOOFED_IPV4};proto=https",
        },
        transport_peer=RENDER_PROXY,
    )
    result = _resolve(request)
    assert result.address == CLIENT_IPV4
    assert result.path == "xff_trusted_chain"


@pytest.mark.unit
def test_ipv6_client_address(proxy_env: None) -> None:
    request = _request(
        peer=RENDER_PROXY,
        headers={"X-Forwarded-For": f"{CLIENT_IPV6}, {CLOUDFLARE_PROXY}"},
        transport_peer=RENDER_PROXY,
    )
    result = _resolve(request)
    assert result.address == CLIENT_IPV6
    assert result.path == "xff_trusted_chain"


@pytest.mark.unit
def test_malformed_xff_falls_back_to_trusted_peer(proxy_env: None) -> None:
    request = _request(
        peer=RENDER_PROXY,
        headers={"X-Forwarded-For": "not-an-ip"},
        transport_peer=RENDER_PROXY,
    )
    result = _resolve(request)
    assert result.address == RENDER_PROXY
    assert result.path == "malformed_forwarding"


@pytest.mark.unit
def test_overlong_chain_falls_back_to_trusted_peer(proxy_env: None) -> None:
    hops = ", ".join(["203.0.113.1"] * 40)
    request = _request(
        peer=RENDER_PROXY,
        headers={"X-Forwarded-For": hops},
        transport_peer=RENDER_PROXY,
    )
    result = _resolve(request)
    assert result.address == RENDER_PROXY
    assert result.path == "malformed_forwarding"


@pytest.mark.unit
def test_empty_xff_elements_are_malformed(proxy_env: None) -> None:
    request = _request(
        peer=RENDER_PROXY,
        headers={"X-Forwarded-For": f"{CLIENT_IPV4}, , {CLOUDFLARE_PROXY}"},
        transport_peer=RENDER_PROXY,
    )
    result = _resolve(request)
    assert result.address == CLIENT_IPV4
    assert result.path == "xff_trusted_chain"


@pytest.mark.unit
def test_missing_peer_returns_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    scope = {
        "type": "http",
        "headers": [],
        "client": None,
        "method": "POST",
        "path": "/admin/login",
        "state": {},
    }
    result = resolve_admin_login_client_source(Request(scope), get_settings())
    assert result.address == "unknown"
    assert result.path == "missing_peer"


@pytest.mark.unit
def test_telemetry_logs_resolution_path_without_raw_addresses(
    proxy_env: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    request = _request(
        peer=RENDER_PROXY,
        headers={
            "X-Forwarded-For": f"{CLIENT_IPV4}, {CLOUDFLARE_PROXY}",
            "CF-Connecting-IP": CLIENT_IPV4,
        },
        transport_peer=RENDER_PROXY,
    )
    with caplog.at_level("INFO"):
        _resolve(request)
    messages = " ".join(record.getMessage() for record in caplog.records)
    assert CLIENT_IPV4 not in messages
    assert CLOUDFLARE_PROXY not in messages
    assert "client source resolved" in messages


@pytest.mark.unit
@pytest.mark.integration
def test_rotating_spoofed_headers_does_not_create_new_source_buckets(
    proxy_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", "operator")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", "x" * 20)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "test-session-secret-32chars-minimum")
    admin_auth.reset_login_rate_limiter()

    from tests.test_admin_auth import FakeRateLimitStore, shared_rate_limiter

    store = FakeRateLimitStore()
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    keys_seen: set[str] = set()

    def capture_try_admit(
        conn: Any,
        *,
        limiter_keys: tuple[str, ...],
        now: Any,
        rate_limit: int,
        window_seconds: int,
        lockout_seconds: int,
    ) -> Any:
        keys_seen.update(limiter_keys)
        return store.try_admit(
            limiter_keys,
            now,
            rate_limit=rate_limit,
            window_seconds=window_seconds,
            lockout_seconds=lockout_seconds,
        )

    request = _request(
        peer=UNTRUSTED_PEER,
        transport_peer=UNTRUSTED_PEER,
    )
    settings = get_settings()

    with shared_rate_limiter(store):
        with patch("app.admin_auth.db.try_admit_admin_login", side_effect=capture_try_admit):
            with patch("app.admin_auth.db.db_connection") as db_conn:
                db_conn.return_value.__enter__.return_value = object()
                db_conn.return_value.__exit__.return_value = None
                for index in range(4):
                    request = _request(
                        peer=UNTRUSTED_PEER,
                        headers={"X-Forwarded-For": f"203.0.113.{index}"},
                        transport_peer=UNTRUSTED_PEER,
                    )
                    admin_auth.try_admit_login_attempt(request, settings, username="ghost")

    assert keys_seen == {admin_auth.build_source_rate_limit_key(UNTRUSTED_PEER)}


@pytest.mark.unit
def test_deployment_render_yaml_proxy_settings_are_consistent() -> None:
    render_yaml = (REPO_ROOT / "render.yaml").read_text(encoding="utf-8")
    assert "--proxy-headers" in render_yaml
    assert "--forwarded-allow-ips=" in render_yaml
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in render_yaml
    assert "ADMIN_CLOUDFLARE_TRUST_ENABLED" in render_yaml
    assert "10.0.0.0/8" in render_yaml


@pytest.mark.unit
def test_health_reports_proxy_trust_summary(proxy_env: None) -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    payload = client.get("/health").json()
    assert payload["admin_proxy_trust"]["trusted_proxy_cidrs_configured"] is True
    assert payload["admin_proxy_trust"]["cloudflare_trust_enabled"] is True
    assert "203.0.113" not in json.dumps(payload)


@pytest.mark.integration
def test_uvicorn_proxy_configuration_resolves_trusted_chain(
    proxy_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("ADMIN_USERNAME", "")
    free_port = _find_free_port()
    env = os.environ.copy()
    env["ADMIN_TRUSTED_PROXY_CIDRS"] = TEST_TRUSTED_CIDRS
    env["ADMIN_CLOUDFLARE_TRUST_ENABLED"] = "true"
    env["ADMIN_CLOUDFLARE_PROXY_CIDRS"] = TEST_CF_CIDRS

    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(free_port),
        "--proxy-headers",
        "--forwarded-allow-ips",
        "127.0.0.1",
    ]
    proc = subprocess.Popen(
        cmd,
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for_health(f"http://127.0.0.1:{free_port}/health")
        with httpx.Client(
            transport=httpx.HTTPTransport(retries=0),
            timeout=5.0,
        ) as http_client:
            response = http_client.get(
                f"http://127.0.0.1:{free_port}/health",
                headers={
                    "X-Forwarded-For": f"{CLIENT_IPV4}, {RENDER_PROXY}",
                },
            )
        assert response.status_code == 200
        assert response.json()["admin_proxy_trust"]["trusted_proxy_cidrs_configured"] is True

        scope = {
            "type": "http",
            "headers": [
                (b"x-forwarded-for", f"{CLIENT_IPV4}, {RENDER_PROXY}".encode("ascii")),
            ],
            "client": (RENDER_PROXY, 12345),
            "method": "GET",
            "path": "/health",
            "state": {"transport_peer": (RENDER_PROXY, 12345)},
        }
        result = resolve_admin_login_client_source(Request(scope), get_settings())
        assert result.address == CLIENT_IPV4
        assert result.path == "xff_trusted_chain"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_health(url: str, *, timeout_seconds: float = 15.0) -> None:
    deadline = time.time() + timeout_seconds
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            response = httpx.get(url, timeout=1.0)
            if response.status_code == 200:
                return
        except Exception as exc:  # noqa: BLE001 - poll until ready
            last_error = exc
        time.sleep(0.2)
    raise RuntimeError(f"service did not become healthy: {last_error}")
