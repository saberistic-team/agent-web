"""Tests for trusted admin login client source resolution (#239)."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app import admin_auth
from app.admin_client_source import (
    ClientSourceResolution,
    normalize_client_address,
    reset_client_source_telemetry,
    resolve_admin_login_client_source,
)
from app.config import get_settings
from app.main import app
from argon2 import PasswordHasher

_REPO_ROOT = Path(__file__).resolve().parent.parent
_RENDER_PROXY_FLAGS = (
    "--proxy-headers",
    "--forwarded-allow-ips",
    "10.0.0.0/8",
)
_TEST_HASH = PasswordHasher().hash("correct-horse-battery-staple")


def _request(
    *,
    peer: str,
    headers: dict[str, str] | None = None,
) -> Request:
    header_list = [
        (name.lower().encode("ascii"), value.encode("ascii"))
        for name, value in (headers or {}).items()
    ]
    scope = {
        "type": "http",
        "headers": header_list,
        "client": (peer, 12345),
        "method": "POST",
        "path": "/admin/login",
    }
    return Request(scope)


def _settings(
    monkeypatch: pytest.MonkeyPatch,
    *,
    trust: bool = True,
    cidrs: str = "10.0.0.0/8,127.0.0.1/32",
) -> Any:
    if trust:
        monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    else:
        monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    if cidrs:
        monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", cidrs)
    else:
        monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    return get_settings()


@pytest.fixture(autouse=True)
def _admin_client_source_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", "operator")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", _TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "test-session-secret-32chars-minimum")
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    reset_client_source_telemetry()
    admin_auth.reset_login_rate_limiter()


@pytest.mark.unit
def test_normalize_client_address_formats() -> None:
    assert normalize_client_address("203.0.113.1") == "203.0.113.1"
    assert normalize_client_address("203.0.113.1:443") == "203.0.113.1"
    assert normalize_client_address("2001:db8::1") == "2001:db8::1"
    assert normalize_client_address("[2001:db8::1]:443") == "2001:db8::1"
    assert normalize_client_address("::ffff:203.0.113.50") == "203.0.113.50"
    assert normalize_client_address("  203.0.113.1  ") == "203.0.113.1"
    assert normalize_client_address("") is None
    assert normalize_client_address("not-an-ip") is None
    assert normalize_client_address("x" * 200) is None


@pytest.mark.unit
def test_direct_spoof_ignored_without_trusted_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    direct = _request(
        peer="198.51.100.10",
        headers={"X-Forwarded-For": "203.0.113.99"},
    )
    assert resolve_admin_login_client_source(direct, settings) == ClientSourceResolution(
        source="198.51.100.10",
        path="untrusted_peer",
    )

    multi = _request(
        peer="198.51.100.10",
        headers={"X-Forwarded-For": "203.0.113.1, 203.0.113.2, 203.0.113.3"},
    )
    assert resolve_admin_login_client_source(multi, settings).source == "198.51.100.10"


@pytest.mark.unit
def test_cloudflare_append_ignores_attacker_leftmost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    request = _request(
        peer="10.1.0.5",
        headers={
            "X-Forwarded-For": "203.0.113.99, 203.0.113.50, 173.245.48.1",
            "CF-Connecting-IP": "203.0.113.50",
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.50"
    assert resolution.path == "cf_connecting_ip"


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    request = _request(
        peer="10.0.0.2",
        headers={"X-Forwarded-For": "203.0.113.77, 10.0.0.2"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.77"
    assert resolution.path == "xff_trusted_chain"


@pytest.mark.unit
def test_partial_trust_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    request = _request(
        peer="198.51.100.20",
        headers={"X-Forwarded-For": "203.0.113.10, 10.0.0.2"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "198.51.100.20"
    assert resolution.path == "untrusted_peer"


@pytest.mark.unit
def test_direct_render_origin_ignores_cloudflare_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    request = _request(
        peer="198.51.100.30",
        headers={
            "CF-Connecting-IP": "203.0.113.88",
            "X-Forwarded-For": "203.0.113.88",
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "198.51.100.30"
    assert resolution.path == "untrusted_peer"


@pytest.mark.unit
def test_header_precedence_cf_over_conflicting_forwarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    request = _request(
        peer="10.0.0.4",
        headers={
            "CF-Connecting-IP": "203.0.113.60",
            "X-Forwarded-For": "203.0.113.60, 173.245.48.2",
            "Forwarded": 'for=203.0.113.70;proto=https, for="[2001:db8::9]"',
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.60"
    assert resolution.path == "cf_connecting_ip"


@pytest.mark.unit
def test_forwarded_header_used_when_cf_path_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    request = _request(
        peer="10.0.0.8",
        headers={
            "Forwarded": 'for=203.0.113.44;proto=https, for="10.0.0.8"',
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.44"
    assert resolution.path == "forwarded_header"


@pytest.mark.unit
def test_malformed_and_overlong_forwarding_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    overlong = ", ".join(f"203.0.113.{index}" for index in range(40))
    request = _request(peer="10.0.0.9", headers={"X-Forwarded-For": overlong})
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "10.0.0.9"
    assert resolution.path in {"trusted_peer", "malformed_forwarding"}

    empty_elements = _request(
        peer="10.0.0.9",
        headers={"X-Forwarded-For": " , , 203.0.113.5 , 10.0.0.9"},
    )
    assert resolve_admin_login_client_source(empty_elements, settings).source == "203.0.113.5"


@pytest.mark.unit
def test_trust_disabled_uses_direct_peer_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, trust=False)
    request = _request(
        peer="10.0.0.1",
        headers={"X-Forwarded-For": "203.0.113.1, 10.0.0.1"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "10.0.0.1"
    assert resolution.path == "direct_peer"


@pytest.mark.unit
@pytest.mark.integration
def test_rotating_spoofed_headers_share_one_limiter_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_admin_auth import (
        FakeRateLimitStore,
        TEST_PASSWORD,
        TEST_USERNAME,
        _login,
        shared_rate_limiter,
        mock_db_connection,
    )

    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "3")
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    store = FakeRateLimitStore()
    with shared_rate_limiter(store):
        for index in range(3):
            response = _login(
                username="ghost",
                password="wrong",
                headers={"X-Forwarded-For": f"203.0.113.{index}"},
            )
            assert response.status_code == 401
        blocked = _login(
            username="ghost",
            password="wrong",
            headers={"X-Forwarded-For": "203.0.113.99"},
        )
    assert blocked.status_code == 429
    assert len(store.rows) == 1


@pytest.mark.unit
def test_render_yaml_proxy_trust_configuration() -> None:
    render_text = (_REPO_ROOT / "render.yaml").read_text(encoding="utf-8")
    for flag in _RENDER_PROXY_FLAGS:
        assert flag in render_text
    assert "ADMIN_TRUST_PROXY_HEADERS" in render_text
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in render_text
    assert "UVICORN_FORWARDED_ALLOW_IPS" in render_text
    assert re.search(
        r"startCommand:.*--proxy-headers.*--forwarded-allow-ips",
        render_text,
    )


@pytest.mark.unit
def test_health_reports_proxy_trust_without_raw_addresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv(
        "ADMIN_TRUSTED_PROXY_CIDRS",
        "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.1/32",
    )
    monkeypatch.setenv(
        "UVICORN_FORWARDED_ALLOW_IPS",
        "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.1",
    )
    response = TestClient(app).get("/health")
    payload = response.json()
    trust = payload["admin_proxy_trust"]
    assert trust["proxy_headers_enabled"] is True
    assert trust["trusted_proxy_configured"] is True
    assert trust["trusted_proxy_network_count"] >= 4
    assert "10.0.0.0" in trust["uvicorn_forwarded_allow_ips"]
    serialized = str(payload)
    assert "203.0.113" not in serialized
    assert "x-forwarded-for" not in serialized.lower()


@pytest.mark.unit
def test_admission_logs_resolution_path_not_raw_ip(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from tests.test_admin_auth import (
        FakeRateLimitStore,
        TEST_USERNAME,
        shared_rate_limiter,
        mock_db_connection,
    )

    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", "10.0.0.0/8")
    store = FakeRateLimitStore()
    trusted_client = TestClient(app, client=("10.0.0.5", 50000))
    caplog.set_level(logging.INFO, logger="app.admin_auth")

    with shared_rate_limiter(store), mock_db_connection():
        form = trusted_client.get("/admin/login")
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', form.text)
        assert csrf is not None
        cookies = {}
        flow = form.cookies.get(admin_auth.LOGIN_FLOW_COOKIE_NAME)
        if flow:
            cookies[admin_auth.LOGIN_FLOW_COOKIE_NAME] = flow
        trusted_client.post(
            "/admin/login",
            data={
                "username": TEST_USERNAME,
                "password": "wrong-password",
                "csrf_token": csrf.group(1),
            },
            cookies=cookies,
            headers={"X-Forwarded-For": "203.0.113.55, 10.0.0.5"},
        )

    path_records = [
        getattr(record, "client_source_path", None)
        for record in caplog.records
        if record.name == "app.admin_auth"
    ]
    assert any(path_records)
    serialized = str(caplog.records) + str(store.rows)
    assert "203.0.113.55" not in serialized


@pytest.mark.integration
def test_uvicorn_proxy_headers_middleware_matches_deployment_flags() -> None:
    """Exercise ProxyHeadersMiddleware with the same allow-ips as render.yaml."""
    import asyncio

    captured: dict[str, str] = {}

    async def echo_app(scope, receive, send):  # noqa: ANN001
        headers = {name.decode().lower(): value.decode() for name, value in scope["headers"]}
        captured["client"] = scope["client"][0]
        captured["xff"] = headers.get("x-forwarded-for", "")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    wrapped = ProxyHeadersMiddleware(
        echo_app,
        trusted_hosts="10.0.0.0/8,127.0.0.1",
    )

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "path": "/",
        "raw_path": b"/",
        "root_path": "",
        "scheme": "http",
        "query_string": b"",
        "headers": [
            (b"x-forwarded-for", b"203.0.113.99, 10.0.0.5"),
            (b"x-forwarded-proto", b"https"),
        ],
        "client": ("10.0.0.5", 54321),
        "server": ("testserver", 80),
    }

    async def receive() -> dict[str, str]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):  # noqa: ANN001
        return None

    asyncio.run(wrapped(scope, receive, send))
    assert captured["client"] == "203.0.113.99"
    assert "10.0.0.5" in captured["xff"]
