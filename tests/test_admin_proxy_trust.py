"""Verified-hop admin login client source resolution (#239)."""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import admin_auth
from app.config import get_settings
from app.main import app
from app.proxy_trust import (
    normalize_ip_address,
    parse_trusted_proxy_networks,
    reset_proxy_trust_telemetry,
    resolve_admin_login_client_source,
)
from tests.test_admin_auth import (
    TEST_PASSWORD,
    TEST_USERNAME,
    FakeRateLimitStore,
    _parse_login_form,
    mock_db_connection,
    shared_rate_limiter,
)

RENDER_TRUSTED_CIDRS = "127.0.0.1/32,10.0.0.0/8,::1/128,fc00::/7"
RENDER_FORWARDED_ALLOW_IPS = "127.0.0.1,10.0.0.0/8,::1,fc00::/7"
TRUSTED_RENDER_PEER = "10.0.0.2"
UNTRUSTED_PEER = "203.0.113.9"


def asgi_with_peer(
    inner_app: FastAPI,
    peer_host: str,
    *,
    peer_port: int = 12345,
) -> Callable[..., Any]:
    """ASGI wrapper that sets the immediate TCP peer for proxy-trust tests."""

    async def wrapped_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            scope = dict(scope)
            scope["client"] = (peer_host, peer_port)
        await inner_app(scope, receive, send)

    return wrapped_app


def _request_with_peer(
    peer_host: str,
    *,
    headers: list[tuple[bytes, bytes]] | None = None,
) -> Any:
    from starlette.requests import Request

    scope = {
        "type": "http",
        "headers": headers or [],
        "client": (peer_host, 12345),
        "method": "POST",
        "path": "/admin/login",
    }
    return Request(scope)


def _enable_proxy_trust(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", RENDER_TRUSTED_CIDRS)


def _login_with_peer(
    peer_host: str,
    *,
    headers: dict[str, str] | None = None,
    password: str = "wrong",
    username: str = TEST_USERNAME,
) -> Any:
    peer_client = TestClient(asgi_with_peer(app, peer_host), follow_redirects=False)
    with mock_db_connection():
        form = peer_client.get("/admin/login")
        csrf_token, cookies = _parse_login_form(form)
        return peer_client.post(
            "/admin/login",
            data={
                "username": username,
                "password": password,
                "csrf_token": csrf_token,
            },
            cookies=cookies,
            headers=headers or {},
        )


@pytest.fixture
def rate_limit_store() -> FakeRateLimitStore:
    return FakeRateLimitStore()


@pytest.fixture(autouse=True)
def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", admin_auth._password_hasher.hash(TEST_PASSWORD))
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "integration-secret-32chars-minimum-value")
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "5")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_WINDOW_SECONDS", "900")
    monkeypatch.setenv("ADMIN_LOGIN_LOCKOUT_SECONDS", "900")
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    admin_auth.reset_login_rate_limiter()
    from tests.test_admin_auth import _login_flows, _session_store

    _login_flows.clear()
    _session_store.clear()


@pytest.fixture(autouse=True)
def _reset_proxy_trust_state() -> None:
    reset_proxy_trust_telemetry()
    admin_auth.reset_login_rate_limiter()


@pytest.mark.unit
def test_direct_spoof_single_and_multi_value_xff_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_proxy_trust(monkeypatch)
    settings = get_settings()
    for header_value in ("203.0.113.50", "203.0.113.50, 198.51.100.20"):
        request = _request_with_peer(
            UNTRUSTED_PEER,
            headers=[(b"x-forwarded-for", header_value.encode("ascii"))],
        )
        resolution = resolve_admin_login_client_source(request, settings)
        assert resolution.source == UNTRUSTED_PEER
        assert resolution.path == "untrusted_peer"


@pytest.mark.unit
def test_cloudflare_append_behavior_ignores_attacker_leftmost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_proxy_trust(monkeypatch)
    settings = get_settings()
    request = _request_with_peer(
        TRUSTED_RENDER_PEER,
        headers=[
            (
                b"x-forwarded-for",
                b"203.0.113.50, 198.51.100.60, 10.0.0.1",
            )
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "198.51.100.60"
    assert resolution.path == "x_forwarded_for"


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_proxy_trust(monkeypatch)
    settings = get_settings()
    request = _request_with_peer(
        TRUSTED_RENDER_PEER,
        headers=[
            (b"cf-connecting-ip", b"203.0.113.77"),
            (b"x-forwarded-for", b"203.0.113.77, 10.0.0.1"),
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.77"
    assert resolution.path == "cf_connecting_ip"


@pytest.mark.unit
def test_partial_trust_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_proxy_trust(monkeypatch)
    settings = get_settings()
    request = _request_with_peer(
        UNTRUSTED_PEER,
        headers=[(b"x-forwarded-for", b"203.0.113.50, 10.0.0.1")],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == UNTRUSTED_PEER
    assert resolution.path == "untrusted_peer"


@pytest.mark.unit
def test_direct_render_origin_ignores_cloudflare_vendor_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_proxy_trust(monkeypatch)
    settings = get_settings()
    request = _request_with_peer(
        UNTRUSTED_PEER,
        headers=[
            (b"cf-connecting-ip", b"203.0.113.88"),
            (b"x-forwarded-for", b"203.0.113.88, 10.0.0.1"),
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == UNTRUSTED_PEER
    assert resolution.path == "untrusted_peer"


@pytest.mark.unit
def test_header_precedence_cf_over_xff_over_forwarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_proxy_trust(monkeypatch)
    settings = get_settings()
    request = _request_with_peer(
        TRUSTED_RENDER_PEER,
        headers=[
            (b"cf-connecting-ip", b"203.0.113.10"),
            (b"x-forwarded-for", b"203.0.113.20, 10.0.0.1"),
            (b"forwarded", b'for=203.0.113.30;proto=https, for=10.0.0.1;proto=https'),
        ],
    )
    assert resolve_admin_login_client_source(request, settings).path == "cf_connecting_ip"

    request = _request_with_peer(
        TRUSTED_RENDER_PEER,
        headers=[
            (b"x-forwarded-for", b"203.0.113.20, 10.0.0.1"),
            (b"forwarded", b'for=203.0.113.30;proto=https, for=10.0.0.1;proto=https'),
        ],
    )
    assert resolve_admin_login_client_source(request, settings).path == "x_forwarded_for"

    request = _request_with_peer(
        TRUSTED_RENDER_PEER,
        headers=[
            (b"forwarded", b'for=203.0.113.30;proto=https, for=10.0.0.1;proto=https'),
        ],
    )
    assert resolve_admin_login_client_source(request, settings).path == "forwarded"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("203.0.113.1", "203.0.113.1"),
        ("203.0.113.1:443", "203.0.113.1"),
        ("2001:db8::1", "2001:db8::1"),
        ("[2001:db8::1]:443", "2001:db8::1"),
        ("::ffff:203.0.113.1", "203.0.113.1"),
        ("  203.0.113.2  ", "203.0.113.2"),
        ("not-an-ip", None),
    ],
)
def test_address_normalization_formats(raw: str, expected: str | None) -> None:
    assert normalize_ip_address(raw) == expected


@pytest.mark.unit
def test_address_normalization_rejects_excessive_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_proxy_trust(monkeypatch)
    settings = get_settings()
    long_chain = ", ".join(["10.0.0.1"] + [f"203.0.113.{index}" for index in range(40)])
    request = _request_with_peer(
        TRUSTED_RENDER_PEER,
        headers=[(b"x-forwarded-for", long_chain.encode("ascii"))],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.path == "trusted_peer_fallback"
    assert resolution.source == TRUSTED_RENDER_PEER


@pytest.mark.unit
@pytest.mark.integration
def test_rotating_spoofed_headers_do_not_create_new_limiter_rows(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    _enable_proxy_trust(monkeypatch)
    with shared_rate_limiter(rate_limit_store):
        for index in range(5):
            response = _login_with_peer(
                UNTRUSTED_PEER,
                username="ghost",
                headers={"X-Forwarded-For": f"203.0.113.{index}"},
            )
            if index < 2:
                assert response.status_code == 401
            elif index == 2:
                assert response.status_code == 429
                break
    assert len(rate_limit_store.rows) == 1
    source_key = admin_auth.build_source_rate_limit_key(UNTRUSTED_PEER)
    assert source_key in rate_limit_store.rows


@pytest.mark.unit
def test_render_yaml_proxy_trust_settings_are_consistent() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    render_yaml = (repo_root / "render.yaml").read_text(encoding="utf-8")
    assert 'ADMIN_TRUST_PROXY_HEADERS' in render_yaml
    assert 'ADMIN_TRUSTED_PROXY_CIDRS' in render_yaml
    assert "--forwarded-allow-ips=" in render_yaml
    assert "--proxy-headers" not in render_yaml
    assert "127.0.0.1/32,10.0.0.0/8" in render_yaml


@pytest.mark.unit
def test_admin_auth_docs_describe_verified_hop_model() -> None:
    docs = (Path(__file__).resolve().parents[1] / "docs" / "ADMIN_AUTH.md").read_text(
        encoding="utf-8"
    )
    assert "Right-to-left" in docs
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in docs
    assert "CF-Connecting-IP" in docs
    assert "admin_client_source_trust" in docs


@pytest.mark.unit
@pytest.mark.integration
def test_privacy_logs_and_limiter_state_exclude_raw_forwarding_data(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    _enable_proxy_trust(monkeypatch)
    caplog.set_level(logging.INFO)
    with shared_rate_limiter(rate_limit_store):
        _login_with_peer(
            TRUSTED_RENDER_PEER,
            headers={
                "CF-Connecting-IP": "203.0.113.55",
                "X-Forwarded-For": "203.0.113.55, 10.0.0.1",
            },
        )
    serialized_rows = json.dumps(rate_limit_store.rows, default=str)
    assert "203.0.113.55" not in serialized_rows
    assert "X-Forwarded-For" not in caplog.text
    assert "CF-Connecting-IP" not in caplog.text
    assert any(
        getattr(record, "source_resolution_path", None) == "cf_connecting_ip"
        for record in caplog.records
    )


@pytest.mark.unit
def test_health_reports_admin_client_source_trust_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_proxy_trust(monkeypatch)
    peer_client = TestClient(app)
    payload = peer_client.get("/health").json()
    trust = payload["admin_client_source_trust"]
    assert trust["resolution_model"] == "verified_hop"
    assert trust["proxy_headers_enabled"] is True
    assert trust["trusted_proxy_cidr_count"] == len(
        parse_trusted_proxy_networks(RENDER_TRUSTED_CIDRS)
    )
    assert trust["uvicorn_proxy_headers_enabled"] is False


@pytest.mark.unit
@pytest.mark.integration
def test_uvicorn_forwarded_allow_ips_matches_deployment_config(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise uvicorn with the same forwarded-allow-ips boundary as render.yaml."""
    _enable_proxy_trust(monkeypatch)
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "5")

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        bound_port = sock.getsockname()[1]

    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=bound_port,
            log_level="warning",
            forwarded_allow_ips=RENDER_FORWARDED_ALLOW_IPS,
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    with (
        shared_rate_limiter(rate_limit_store),
        patch("app.main.db.init_db"),
        mock_db_connection(),
    ):
        thread.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not server.started:
            time.sleep(0.05)
        assert server.started

        with httpx.Client(base_url=f"http://127.0.0.1:{bound_port}", timeout=10.0) as client:
            form = client.get("/admin/login")
            csrf_token, cookies = _parse_login_form(form)
            first = client.post(
                "/admin/login",
                data={
                    "username": "ghost",
                    "password": "wrong",
                    "csrf_token": csrf_token,
                },
                cookies=cookies,
                headers={"X-Forwarded-For": "203.0.113.60, 127.0.0.1"},
            )
            form = client.get("/admin/login")
            csrf_token, cookies = _parse_login_form(form)
            second = client.post(
                "/admin/login",
                data={
                    "username": "ghost",
                    "password": "wrong",
                    "csrf_token": csrf_token,
                },
                cookies=cookies,
                headers={"X-Forwarded-For": "203.0.113.61, 127.0.0.1"},
            )

        server.should_exit = True
        thread.join(timeout=5)

    assert first.status_code == 401
    assert second.status_code == 401
    assert len(rate_limit_store.rows) == 2


@pytest.mark.unit
@pytest.mark.integration
def test_trusted_peer_limiter_uses_cf_connecting_ip_not_spoofed_xff(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    _enable_proxy_trust(monkeypatch)
    with shared_rate_limiter(rate_limit_store):
        assert (
            _login_with_peer(
                TRUSTED_RENDER_PEER,
                username="ghost",
                headers={
                    "CF-Connecting-IP": "203.0.113.70",
                    "X-Forwarded-For": "203.0.113.99, 203.0.113.70, 10.0.0.1",
                },
            ).status_code
            == 401
        )
        assert (
            _login_with_peer(
                TRUSTED_RENDER_PEER,
                username="ghost",
                headers={
                    "CF-Connecting-IP": "203.0.113.70",
                    "X-Forwarded-For": "203.0.113.88, 203.0.113.70, 10.0.0.1",
                },
            ).status_code
            == 401
        )
        blocked = _login_with_peer(
            TRUSTED_RENDER_PEER,
            username="ghost",
            headers={
                "CF-Connecting-IP": "203.0.113.70",
                "X-Forwarded-For": "203.0.113.77, 203.0.113.70, 10.0.0.1",
            },
        )
    assert blocked.status_code == 429
    source_key = admin_auth.build_source_rate_limit_key("203.0.113.70")
    assert source_key in rate_limit_store.rows
    assert all(key == source_key for key in rate_limit_store.rows)
