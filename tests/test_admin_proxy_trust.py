"""Tests for secure admin login client source resolution (#239)."""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app import admin_auth
from app.admin_client_source import (
    SourceResolutionPath,
    normalize_ip_address,
    reset_source_resolution_telemetry,
    resolve_admin_login_client_source,
)
from app.config import get_settings
from app.main import app
from tests.test_admin_auth import (
    FakeRateLimitStore,
    TEST_HASH,
    TEST_PASSWORD,
    TEST_USERNAME,
    _login,
    mock_db_connection,
    shared_rate_limiter,
)

pytest_plugins = ["tests.test_admin_auth"]

REPO_ROOT = Path(__file__).resolve().parents[1]
RENDER_TRUSTED_CIDRS = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
PRODUCTION_CLOUDFLARE_CIDRS = (
    "173.245.48.0/20,103.21.244.0/22,103.22.200.0/22,103.31.4.0/22,"
    "141.101.64.0/18,108.162.192.0/18,190.93.240.0/20,188.114.96.0/20,"
    "197.234.240.0/22,198.41.128.0/17,162.158.0.0/15,104.16.0.0/13,"
    "104.24.0.0/14,172.64.0.0/13,131.0.72.0/22"
)
TEST_CLOUDFLARE_CIDRS = "198.51.100.0/24"
TEST_CLOUDFLARE_EDGE = "198.51.100.10"
TEST_RENDER_PEER = "10.0.0.5"
TEST_CLIENT_IP = "203.0.113.50"


def _request_with_client(host: str, headers: dict[str, str] | None = None) -> Request:
    header_list = [
        (key.lower().encode("ascii"), value.encode("ascii"))
        for key, value in (headers or {}).items()
    ]
    scope = {
        "type": "http",
        "headers": header_list,
        "client": (host, 12345),
        "method": "POST",
        "path": "/admin/login",
    }
    return Request(scope)


@pytest.fixture(autouse=True)
def proxy_trust_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "test-session-secret-32chars-minimum")
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    monkeypatch.delenv("ADMIN_CLOUDFLARE_TRUSTED_CIDRS", raising=False)
    admin_auth.reset_login_rate_limiter()
    reset_source_resolution_telemetry()


@pytest.mark.unit
def test_normalize_ip_address_formats() -> None:
    assert normalize_ip_address("203.0.113.1") == "203.0.113.1"
    assert normalize_ip_address("203.0.113.1:443") == "203.0.113.1"
    assert normalize_ip_address("2001:db8::1") == "2001:db8::1"
    assert normalize_ip_address("[2001:db8::1]:443") == "2001:db8::1"
    assert normalize_ip_address("::ffff:203.0.113.1") == "203.0.113.1"
    assert normalize_ip_address("  203.0.113.2  ") == "203.0.113.2"
    assert normalize_ip_address("") is None
    assert normalize_ip_address("not-an-ip") is None
    assert normalize_ip_address("999.999.999.999") is None


@pytest.mark.unit
def test_direct_spoof_single_and_multi_value_xff_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    direct_peer = "198.51.100.44"
    for headers in (
        {"X-Forwarded-For": "203.0.113.99"},
        {"X-Forwarded-For": "203.0.113.1, 203.0.113.2, 203.0.113.3"},
        {
            "X-Forwarded-For": "203.0.113.1",
            "Forwarded": 'for="203.0.113.9"',
            "CF-Connecting-IP": "203.0.113.8",
        },
    ):
        request = _request_with_client(direct_peer, headers)
        assert resolve_admin_login_client_source(request, settings) == direct_peer


@pytest.mark.unit
def test_cloudflare_append_ignores_attacker_leftmost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", TEST_RENDER_PEER)
    monkeypatch.setenv("ADMIN_CLOUDFLARE_TRUSTED_CIDRS", TEST_CLOUDFLARE_CIDRS)
    settings = get_settings()
    request = _request_with_client(
        TEST_RENDER_PEER,
        {
            "X-Forwarded-For": (
                f"203.0.113.1, {TEST_CLIENT_IP}, {TEST_CLOUDFLARE_EDGE}"
            ),
        },
    )
    assert resolve_admin_login_client_source(request, settings) == TEST_CLIENT_IP


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", RENDER_TRUSTED_CIDRS)
    monkeypatch.setenv("ADMIN_CLOUDFLARE_TRUSTED_CIDRS", TEST_CLOUDFLARE_CIDRS)
    settings = get_settings()
    request = _request_with_client(
        TEST_RENDER_PEER,
        {
            "X-Forwarded-For": f"{TEST_CLIENT_IP}, {TEST_CLOUDFLARE_EDGE}, {TEST_RENDER_PEER}",
            "CF-Connecting-IP": TEST_CLIENT_IP,
        },
    )
    assert resolve_admin_login_client_source(request, settings) == TEST_CLIENT_IP


@pytest.mark.unit
def test_partial_trust_untrusted_intermediary_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", TEST_RENDER_PEER)
    settings = get_settings()
    request = _request_with_client(
        "203.0.113.200",
        {"X-Forwarded-For": f"{TEST_CLIENT_IP}, {TEST_RENDER_PEER}"},
    )
    assert resolve_admin_login_client_source(request, settings) == "203.0.113.200"


@pytest.mark.unit
def test_direct_render_origin_ignores_cloudflare_vendor_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    direct_peer = "203.0.113.77"
    request = _request_with_client(
        direct_peer,
        {
            "CF-Connecting-IP": "203.0.113.1",
            "X-Forwarded-For": "203.0.113.1",
        },
    )
    assert resolve_admin_login_client_source(request, settings) == direct_peer


@pytest.mark.unit
def test_header_precedence_cf_over_conflicting_forwarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", TEST_RENDER_PEER)
    monkeypatch.setenv("ADMIN_CLOUDFLARE_TRUSTED_CIDRS", TEST_CLOUDFLARE_CIDRS)
    settings = get_settings()
    request = _request_with_client(
        TEST_RENDER_PEER,
        {
            "CF-Connecting-IP": TEST_CLIENT_IP,
            "X-Forwarded-For": f"203.0.113.9, {TEST_CLOUDFLARE_EDGE}",
            "Forwarded": 'for="203.0.113.8"',
        },
    )
    assert resolve_admin_login_client_source(request, settings) == TEST_CLIENT_IP


@pytest.mark.unit
def test_forwarded_header_used_when_xff_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", TEST_RENDER_PEER)
    settings = get_settings()
    request = _request_with_client(
        TEST_RENDER_PEER,
        {"Forwarded": f'for="{TEST_CLIENT_IP}", for={TEST_RENDER_PEER}'},
    )
    assert resolve_admin_login_client_source(request, settings) == TEST_CLIENT_IP


@pytest.mark.unit
def test_overlong_forwarding_chain_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", TEST_RENDER_PEER)
    settings = get_settings()
    hops = ", ".join(f"203.0.113.{index}" for index in range(25))
    request = _request_with_client(TEST_RENDER_PEER, {"X-Forwarded-For": hops})
    assert resolve_admin_login_client_source(request, settings) == TEST_RENDER_PEER


@pytest.mark.unit
def test_telemetry_contains_no_raw_addresses(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", TEST_RENDER_PEER)
    settings = get_settings()
    caplog.set_level(logging.INFO)
    reset_source_resolution_telemetry()
    request = _request_with_client(
        TEST_RENDER_PEER,
        {"X-Forwarded-For": f"{TEST_CLIENT_IP}, {TEST_RENDER_PEER}"},
    )
    resolve_admin_login_client_source(request, settings)
    assert caplog.records
    joined = " ".join(record.getMessage() for record in caplog.records)
    assert TEST_CLIENT_IP not in joined
    assert TEST_RENDER_PEER not in joined
    assert any(
        getattr(record, "source_resolution_path", None) == SourceResolutionPath.TRUSTED_XFF.value
        for record in caplog.records
    )


@pytest.mark.unit
@pytest.mark.integration
def test_rotating_spoofed_headers_does_not_create_new_limiter_rows(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "5")
    with shared_rate_limiter(rate_limit_store):
        for index in range(8):
            response = _login(
                password="wrong",
                headers={"X-Forwarded-For": f"203.0.113.{index}"},
            )
            if index < 5:
                assert response.status_code == 401
            else:
                assert response.status_code == 429
    assert len(rate_limit_store.rows) == 2


@pytest.mark.unit
def test_render_yaml_proxy_trust_settings_are_consistent() -> None:
    render_yaml = (REPO_ROOT / "render.yaml").read_text(encoding="utf-8")
    assert "--proxy-headers" in render_yaml
    assert "--forwarded-allow-ips=" in render_yaml
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in render_yaml
    assert "ADMIN_CLOUDFLARE_TRUSTED_CIDRS" in render_yaml

    allow_ips_match = re.search(
        r"--forwarded-allow-ips=([^\s]+)",
        render_yaml,
    )
    trusted_match = re.search(
        r'key: ADMIN_TRUSTED_PROXY_CIDRS\s+value: "([^"]+)"',
        render_yaml,
    )
    cloudflare_match = re.search(
        r"key: ADMIN_CLOUDFLARE_TRUSTED_CIDRS\s+value: >-\s+([\s\S]*?)(?:\n\s+- key:|\Z)",
        render_yaml,
    )
    assert allow_ips_match is not None
    assert trusted_match is not None
    assert cloudflare_match is not None
    allow_ips = allow_ips_match.group(1)
    assert trusted_match.group(1) in allow_ips
    cloudflare_cidrs = re.sub(r"\s+", "", cloudflare_match.group(1))
    for cidr in cloudflare_cidrs.split(","):
        assert cidr in allow_ips


@pytest.mark.unit
def test_admin_auth_docs_describe_proxy_trust_model() -> None:
    docs = (REPO_ROOT / "docs" / "ADMIN_AUTH.md").read_text(encoding="utf-8")
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in docs
    assert "ADMIN_CLOUDFLARE_TRUSTED_CIDRS" in docs
    assert "right-to-left" in docs
    assert "forwarded-allow-ips" in docs
    assert "ADMIN_TRUST_PROXY_HEADERS" not in docs


def _proxy_headers_client(forwarded_allow_ips: str) -> TestClient:
    wrapped = ProxyHeadersMiddleware(app, trusted_hosts=forwarded_allow_ips)
    return TestClient(wrapped, follow_redirects=False)


@pytest.mark.integration
def test_uvicorn_proxy_chain_resolves_client_for_limiter(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_trusted = f"testclient,127.0.0.1,{RENDER_TRUSTED_CIDRS},{TEST_CLOUDFLARE_CIDRS}"
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", local_trusted)
    monkeypatch.setenv("ADMIN_CLOUDFLARE_TRUSTED_CIDRS", TEST_CLOUDFLARE_CIDRS)
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")

    with shared_rate_limiter(rate_limit_store), mock_db_connection():
        http_client = _proxy_headers_client(local_trusted)
        login_page = http_client.get("/admin/login")
        assert login_page.status_code == 200
        csrf_match = re.search(
            r'name="csrf_token" value="([^"]+)"',
            login_page.text,
        )
        flow_cookie = login_page.cookies.get("admin_login_flow")
        assert csrf_match is not None
        assert flow_cookie is not None

        trusted_headers = {
            "X-Forwarded-For": (
                f"203.0.113.1, {TEST_CLIENT_IP}, "
                f"{TEST_CLOUDFLARE_EDGE}, testclient"
            ),
            "CF-Connecting-IP": TEST_CLIENT_IP,
        }
        for _ in range(2):
            response = http_client.post(
                "/admin/login",
                data={
                    "username": "ghost",
                    "password": "wrong",
                    "csrf_token": csrf_match.group(1),
                },
                cookies={"admin_login_flow": flow_cookie},
                headers=trusted_headers,
            )
            assert response.status_code == 401
            csrf_match = re.search(
                r'name="csrf_token" value="([^"]+)"',
                response.text,
            )
            set_cookie = response.headers.get("set-cookie", "")
            flow_cookie_match = re.search(r"admin_login_flow=([^;]+)", set_cookie)
            if flow_cookie_match is not None:
                flow_cookie = flow_cookie_match.group(1)
            assert csrf_match is not None

        blocked = http_client.post(
            "/admin/login",
            data={
                "username": "ghost",
                "password": "wrong",
                "csrf_token": csrf_match.group(1),
            },
            cookies={"admin_login_flow": flow_cookie},
            headers={
                **trusted_headers,
                "X-Forwarded-For": (
                    f"203.0.113.9, {TEST_CLIENT_IP}, "
                    f"{TEST_CLOUDFLARE_EDGE}, testclient"
                ),
            },
        )
        assert blocked.status_code == 429

    source_key = admin_auth.build_source_rate_limit_key(TEST_CLIENT_IP)
    assert source_key in rate_limit_store.rows
    assert len(rate_limit_store.rows) == 1
