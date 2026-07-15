"""Tests for trusted-proxy admin login client source resolution (#239)."""

from __future__ import annotations

import re
import socket
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app import admin_auth
from app.admin_client_source import (
    ClientSourceResolutionPath,
    DEFAULT_PRODUCTION_TRUSTED_PROXY_CIDRS,
    normalize_ip_address,
    resolve_admin_login_client_source,
)
from app.config import get_settings

_TEST_TRUSTED_CIDRS = "10.0.0.0/8,203.0.113.0/30"
_RENDER_LB = "10.0.0.5"
_CLOUDFLARE_EDGE = "203.0.113.1"
_CLIENT = "198.51.100.42"
_DIRECT_PEER = "198.51.100.99"


def _request(
    *,
    peer: str,
    headers: dict[str, str] | None = None,
) -> Request:
    header_list = [
        (key.lower().encode("ascii"), value.encode("ascii"))
        for key, value in (headers or {}).items()
    ]
    scope = {
        "type": "http",
        "headers": header_list,
        "client": (peer, 12345),
        "method": "POST",
        "path": "/admin/login",
    }
    return Request(scope)


@pytest.fixture
def trusted_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", _TEST_TRUSTED_CIDRS)
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)


@pytest.mark.unit
def test_normalize_ipv4_and_ipv6() -> None:
    assert normalize_ip_address("203.0.113.1") == "203.0.113.1"
    assert normalize_ip_address(" 203.0.113.1:443 ") == "203.0.113.1"
    assert normalize_ip_address("2001:db8::1") == "2001:db8::1"
    assert normalize_ip_address("::ffff:203.0.113.50") == "203.0.113.50"
    assert normalize_ip_address("not-an-ip") is None
    assert normalize_ip_address("") is None


@pytest.mark.unit
def test_direct_spoof_single_and_multi_xff_ignored(trusted_proxy_env: None) -> None:
    settings = get_settings()
    for header_value in ("203.0.113.77", "203.0.113.77, 203.0.113.88"):
        request = _request(peer=_DIRECT_PEER, headers={"X-Forwarded-For": header_value})
        resolution = resolve_admin_login_client_source(request, settings)
        assert resolution.source == _DIRECT_PEER
        assert resolution.path == ClientSourceResolutionPath.UNTRUSTED_HEADERS_REJECTED


@pytest.mark.unit
def test_cloudflare_append_ignores_prepended_leftmost(trusted_proxy_env: None) -> None:
    settings = get_settings()
    xff = f"203.0.113.99, {_CLIENT}, {_CLOUDFLARE_EDGE}"
    request = _request(peer=_RENDER_LB, headers={"X-Forwarded-For": xff})
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == _CLIENT
    assert resolution.path == ClientSourceResolutionPath.FORWARDED_CHAIN


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client(trusted_proxy_env: None) -> None:
    settings = get_settings()
    xff = f"{_CLIENT}, {_CLOUDFLARE_EDGE}"
    request = _request(peer=_RENDER_LB, headers={"X-Forwarded-For": xff})
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == _CLIENT
    assert resolution.path == ClientSourceResolutionPath.FORWARDED_CHAIN


@pytest.mark.unit
def test_partial_trust_untrusted_intermediary_fails_closed(trusted_proxy_env: None) -> None:
    settings = get_settings()
    untrusted_mid = "198.51.100.200"
    xff = f"{_CLIENT}, {_RENDER_LB}"
    request = _request(peer=untrusted_mid, headers={"X-Forwarded-For": xff})
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == untrusted_mid
    assert resolution.path == ClientSourceResolutionPath.UNTRUSTED_HEADERS_REJECTED


@pytest.mark.unit
def test_direct_render_origin_ignores_cf_connecting_ip(trusted_proxy_env: None) -> None:
    settings = get_settings()
    request = _request(
        peer=_DIRECT_PEER,
        headers={"CF-Connecting-IP": "203.0.113.55"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == _DIRECT_PEER
    assert resolution.path == ClientSourceResolutionPath.UNTRUSTED_HEADERS_REJECTED


@pytest.mark.unit
def test_header_precedence_xff_over_forwarded_and_cf(trusted_proxy_env: None) -> None:
    settings = get_settings()
    request = _request(
        peer=_RENDER_LB,
        headers={
            "X-Forwarded-For": f"{_CLIENT}, {_CLOUDFLARE_EDGE}",
            "Forwarded": 'for="203.0.113.88"',
            "CF-Connecting-IP": "203.0.113.77",
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == _CLIENT
    assert resolution.path == ClientSourceResolutionPath.FORWARDED_CHAIN


@pytest.mark.unit
def test_forwarded_header_used_when_xff_absent(trusted_proxy_env: None) -> None:
    settings = get_settings()
    request = _request(
        peer=_RENDER_LB,
        headers={"Forwarded": f'for="{_CLIENT}";proto=https'},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == _CLIENT
    assert resolution.path == ClientSourceResolutionPath.FORWARDED_HEADER


@pytest.mark.unit
def test_cf_connecting_ip_used_when_only_vendor_header_present(
    trusted_proxy_env: None,
) -> None:
    settings = get_settings()
    request = _request(
        peer=_RENDER_LB,
        headers={"CF-Connecting-IP": _CLIENT},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == _CLIENT
    assert resolution.path == ClientSourceResolutionPath.CF_CONNECTING_IP


@pytest.mark.unit
def test_malformed_and_overlong_chains_fail_closed(trusted_proxy_env: None) -> None:
    settings = get_settings()
    overlong = ", ".join([f"10.0.0.{index}" for index in range(40)])
    request = _request(peer=_RENDER_LB, headers={"X-Forwarded-For": overlong})
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == _RENDER_LB
    assert resolution.path == ClientSourceResolutionPath.UNTRUSTED_HEADERS_REJECTED

    empty_element = f"{_CLIENT}, , {_CLOUDFLARE_EDGE}"
    request = _request(peer=_RENDER_LB, headers={"X-Forwarded-For": empty_element})
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.path == ClientSourceResolutionPath.UNTRUSTED_HEADERS_REJECTED


@pytest.mark.unit
def test_missing_peer_returns_unknown() -> None:
    settings = get_settings()
    scope = {
        "type": "http",
        "headers": [],
        "client": None,
        "method": "POST",
        "path": "/admin/login",
    }
    request = Request(scope)
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "unknown"
    assert resolution.path == ClientSourceResolutionPath.UNKNOWN


@pytest.mark.unit
def test_legacy_admin_trust_proxy_headers_enables_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    settings = get_settings()
    xff = f"{_CLIENT}, 108.162.192.10"
    request = _request(peer=_RENDER_LB, headers={"X-Forwarded-For": xff})
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == _CLIENT
    assert resolution.path == ClientSourceResolutionPath.FORWARDED_CHAIN
    assert "108.162.192.0/18" in DEFAULT_PRODUCTION_TRUSTED_PROXY_CIDRS


@pytest.fixture
def rate_limit_store() -> Any:
    from test_admin_auth import FakeRateLimitStore as AuthStore

    return AuthStore()


@pytest.fixture
def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from argon2 import PasswordHasher

    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", "operator")
    monkeypatch.setenv(
        "ADMIN_PASSWORD_HASH",
        PasswordHasher().hash("correct-horse-battery-staple"),
    )
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "test-session-secret-32chars-minimum")
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "5")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_WINDOW_SECONDS", "900")
    monkeypatch.setenv("ADMIN_LOGIN_LOCKOUT_SECONDS", "900")
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    admin_auth.reset_login_rate_limiter()


@pytest.mark.unit
@pytest.mark.integration
def test_rotating_spoofed_headers_share_one_limiter_bucket(
    monkeypatch: pytest.MonkeyPatch,
    rate_limit_store: Any,
    admin_env: None,
) -> None:
    from test_admin_auth import mock_db_connection, shared_rate_limiter

    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    admin_auth.reset_login_rate_limiter()

    from app.main import app as main_app

    client = TestClient(main_app, follow_redirects=False)
    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            form = client.get("/admin/login")
            csrf = re.search(r'name="csrf_token" value="([^"]+)"', form.text)
            assert csrf is not None
            cookies = {}
            flow = form.cookies.get(admin_auth.LOGIN_FLOW_COOKIE_NAME)
            if flow:
                cookies[admin_auth.LOGIN_FLOW_COOKIE_NAME] = flow

            for index in range(2):
                response = client.post(
                    "/admin/login",
                    data={
                        "username": "ghost",
                        "password": "wrong",
                        "csrf_token": csrf.group(1),
                    },
                    cookies=cookies,
                    headers={"X-Forwarded-For": f"203.0.113.{index}"},
                )
                assert response.status_code == 401
                csrf = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
                assert csrf is not None
                flow = response.cookies.get(admin_auth.LOGIN_FLOW_COOKIE_NAME)
                if flow:
                    cookies[admin_auth.LOGIN_FLOW_COOKIE_NAME] = flow

            blocked = client.post(
                "/admin/login",
                data={
                    "username": "ghost",
                    "password": "wrong",
                    "csrf_token": csrf.group(1),
                },
                cookies=cookies,
                headers={"X-Forwarded-For": "203.0.113.99"},
            )
    assert blocked.status_code == 429
    source_key = admin_auth.build_source_rate_limit_key("testclient")
    assert len(rate_limit_store.rows) == 1
    assert source_key in rate_limit_store.rows


@pytest.mark.integration
def test_uvicorn_proxy_headers_with_trusted_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise Uvicorn ProxyHeadersMiddleware with the production allowlist."""
    monkeypatch.setenv(
        "ADMIN_TRUSTED_PROXY_CIDRS",
        f"{_TEST_TRUSTED_CIDRS},127.0.0.0/8",
    )
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)

    probe_app = FastAPI()

    @probe_app.get("/_test/client-source")
    def probe(request: Request) -> JSONResponse:
        settings = get_settings()
        resolution = resolve_admin_login_client_source(request, settings)
        return JSONResponse(
            {
                "source": resolution.source,
                "path": resolution.path.value,
            }
        )

    wrapped = ProxyHeadersMiddleware(probe_app, trusted_hosts="127.0.0.0/8")

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    config = uvicorn.Config(
        wrapped,
        host="127.0.0.1",
        port=port,
        log_level="error",
        forwarded_allow_ips="127.0.0.0/8",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)
    else:
        server.should_exit = True
        pytest.fail("uvicorn test server did not start")

    try:
        local_hop = "127.0.0.1"
        xff = f"203.0.113.99, {_CLIENT}, {local_hop}"
        response = httpx.get(
            f"http://127.0.0.1:{port}/_test/client-source",
            headers={"X-Forwarded-For": xff},
            timeout=5.0,
        )
    finally:
        server.should_exit = True
        thread.join(timeout=5.0)

    assert response.status_code == 200
    payload = response.json()
    assert payload["source"] == _CLIENT
    assert payload["path"] == ClientSourceResolutionPath.FORWARDED_CHAIN.value


@pytest.mark.unit
def test_health_reports_proxy_trust_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.main import app as main_app

    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", _TEST_TRUSTED_CIDRS)
    monkeypatch.setenv("UVICORN_FORWARDED_ALLOW_IPS", "10.0.0.0/8")
    client = TestClient(main_app)
    payload = client.get("/health").json()
    assert payload["proxy_trust"]["trusted_proxy_cidrs_configured"] is True
    assert payload["proxy_trust"]["uvicorn_forwarded_allow_ips_configured"] is True


@pytest.mark.unit
def test_render_yaml_proxy_settings_are_consistent() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    render_yaml = (repo_root / "render.yaml").read_text(encoding="utf-8")
    assert "UVICORN_FORWARDED_ALLOW_IPS" in render_yaml
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in render_yaml
    assert "--forwarded-allow-ips" in render_yaml
    assert "10.0.0.0/8" in render_yaml
    assert "173.245.48.0/20" in render_yaml


@pytest.mark.unit
def test_telemetry_and_limiter_keys_contain_no_raw_forwarding_data(
    trusted_proxy_env: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = get_settings()
    xff = f"203.0.113.99, {_CLIENT}, {_CLOUDFLARE_EDGE}"
    request = _request(peer=_RENDER_LB, headers={"X-Forwarded-For": xff})
    with caplog.at_level("INFO"):
        resolution = resolve_admin_login_client_source(request, settings)
        admin_auth.build_source_rate_limit_key(resolution.source)

    combined_logs = " ".join(record.message for record in caplog.records)
    assert _CLIENT not in combined_logs
    assert xff not in combined_logs
    assert "x-forwarded-for" not in combined_logs.lower()

    limiter_key = admin_auth.build_source_rate_limit_key(resolution.source)
    assert _CLIENT not in limiter_key
    assert len(limiter_key) == 64
