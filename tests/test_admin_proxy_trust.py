"""Tests for trusted-proxy admin login client source resolution (#239)."""

from __future__ import annotations

import asyncio
import logging
import re
import socket
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest
import uvicorn
from fastapi import Request
from fastapi.testclient import TestClient

from app import admin_auth
from app.config import get_settings
from app.main import app
from app.proxy_trust import (
    ClientSourceResolution,
    normalize_client_address,
    reset_proxy_trust_telemetry,
    resolve_admin_login_client_source,
)
from tests.test_admin_auth import (
    FakeRateLimitStore,
    _login,
    _request_with_client,
    shared_rate_limiter,
)

pytest_plugins = ("tests.test_admin_auth",)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]
RENDER_YAML = REPO_ROOT / "render.yaml"
ADMIN_AUTH_DOC = REPO_ROOT / "docs" / "ADMIN_AUTH.md"

# Simulated Render load-balancer peer used in trusted-proxy tests.
RENDER_LB = "10.0.0.55"
RENDER_TRUST = "10.0.0.0/8"
CF_EGRESS = "172.68.10.20"
CF_TRUST = "172.64.0.0/13"


def _trusted_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", RENDER_TRUST)
    monkeypatch.setenv("ADMIN_TRUSTED_CLOUDFLARE_IPS", CF_TRUST)
    monkeypatch.setenv("ADMIN_TRUST_CLOUDFLARE_EDGE", "true")


def _append_header(request: Request, name: str, value: str) -> None:
    request.headers.__dict__["_list"].append((name.lower().encode(), value.encode()))


def _resolve(
    *,
    peer: str,
    xff: str | None = None,
    forwarded: str | None = None,
    cf_connecting_ip: str | None = None,
    monkeypatch: pytest.MonkeyPatch,
) -> ClientSourceResolution:
    _trusted_env(monkeypatch)
    settings = get_settings()
    request = _request_with_client(peer)
    if xff is not None:
        _append_header(request, "x-forwarded-for", xff)
    if forwarded is not None:
        _append_header(request, "forwarded", forwarded)
    if cf_connecting_ip is not None:
        _append_header(request, "cf-connecting-ip", cf_connecting_ip)
    return resolve_admin_login_client_source(request, settings)


class _ImmediatePeerMiddleware:
    """Test-only ASGI middleware to set the TCP peer Render would expose."""

    def __init__(self, app: Any, peer_host: str) -> None:
        self.app = app
        self.peer_host = peer_host

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            scope = dict(scope)
            scope["client"] = (self.peer_host, 54321)
        await self.app(scope, receive, send)


def _client_behind_render(peer_host: str = RENDER_LB) -> TestClient:
    wrapped = _ImmediatePeerMiddleware(app, peer_host)
    return TestClient(wrapped, follow_redirects=False)


@pytest.fixture(autouse=True)
def _reset_proxy_telemetry() -> None:
    reset_proxy_trust_telemetry()


def test_direct_spoof_single_xff_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_IPS", raising=False)
    settings = get_settings()
    request = _request_with_client("198.51.100.10")
    _append_header(request, "x-forwarded-for", "203.0.113.99")
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution == ClientSourceResolution("198.51.100.10", "direct_peer")


def test_direct_spoof_multi_xff_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_IPS", raising=False)
    settings = get_settings()
    request = _request_with_client("198.51.100.10")
    _append_header(request, "x-forwarded-for", "203.0.113.1, 203.0.113.2, 203.0.113.3")
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "198.51.100.10"
    assert resolution.path == "direct_peer"


def test_cloudflare_append_ignores_leftmost_spoof(monkeypatch: pytest.MonkeyPatch) -> None:
    resolution = _resolve(
        peer=RENDER_LB,
        xff="203.0.113.99, 198.51.100.5",
        monkeypatch=monkeypatch,
    )
    assert resolution == ClientSourceResolution("198.51.100.5", "forwarded_chain")


def test_trusted_chain_resolves_client_behind_render_and_cloudflare(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolution = _resolve(
        peer=RENDER_LB,
        xff=f"203.0.113.50, {CF_EGRESS}, {RENDER_LB}",
        monkeypatch=monkeypatch,
    )
    assert resolution == ClientSourceResolution("203.0.113.50", "forwarded_chain")


def test_partial_trust_untrusted_intermediary_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", RENDER_TRUST)
    settings = get_settings()
    request = _request_with_client("198.51.100.99")
    _append_header(request, "x-forwarded-for", f"203.0.113.1, {RENDER_LB}")
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution == ClientSourceResolution("198.51.100.99", "direct_peer")


def test_direct_render_origin_ignores_spoofed_cf_connecting_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolution = _resolve(
        peer=RENDER_LB,
        xff="203.0.113.77",
        cf_connecting_ip="203.0.113.99",
        monkeypatch=monkeypatch,
    )
    assert resolution.source == "203.0.113.77"
    assert resolution.path == "forwarded_chain"


def test_header_precedence_xff_over_forwarded_and_cf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolution = _resolve(
        peer=RENDER_LB,
        xff=f"203.0.113.10, {CF_EGRESS}",
        forwarded='for=203.0.113.88;proto=https',
        cf_connecting_ip="203.0.113.99",
        monkeypatch=monkeypatch,
    )
    assert resolution.source == "203.0.113.10"


def test_forwarded_header_used_when_xff_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    resolution = _resolve(
        peer=RENDER_LB,
        forwarded='for=203.0.113.44;proto=https, for="[2001:db8::5]"',
        monkeypatch=monkeypatch,
    )
    assert resolution.source == "2001:db8::5"
    assert resolution.path == "forwarded_rfc7239"


def test_cf_connecting_ip_when_edge_hop_present_and_agrees(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolution = _resolve(
        peer=RENDER_LB,
        xff=f"203.0.113.60, {CF_EGRESS}",
        cf_connecting_ip="203.0.113.60",
        monkeypatch=monkeypatch,
    )
    assert resolution.path == "cf_connecting_ip"
    assert resolution.source == "203.0.113.60"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("203.0.113.1", "203.0.113.1"),
        ("203.0.113.1:8080", "203.0.113.1"),
        ("2001:db8::1", "2001:db8::1"),
        ("[2001:db8::1]:443", "2001:db8::1"),
        ("::ffff:203.0.113.7", "203.0.113.7"),
        ("  203.0.113.2  ", "203.0.113.2"),
        ("", None),
        ("not-an-ip", None),
    ],
)
def test_normalize_client_address(raw: str, expected: str | None) -> None:
    assert normalize_client_address(raw) == expected


def test_empty_xff_elements_and_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    resolution = _resolve(
        peer=RENDER_LB,
        xff=" 203.0.113.3 , , ",
        monkeypatch=monkeypatch,
    )
    assert resolution.source == "203.0.113.3"


def test_overlong_forward_chain_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    hops = ", ".join(f"203.0.113.{i}" for i in range(25))
    resolution = _resolve(peer=RENDER_LB, xff=hops, monkeypatch=monkeypatch)
    assert resolution == ClientSourceResolution("unknown", "malformed_fallback")


def test_missing_peer_unknown_bucket() -> None:
    scope = {
        "type": "http",
        "headers": [],
        "client": None,
        "method": "POST",
        "path": "/admin/login",
    }
    settings = get_settings()
    resolution = resolve_admin_login_client_source(Request(scope), settings)
    assert resolution == ClientSourceResolution("unknown", "missing_peer")


@pytest.mark.integration
def test_rotating_spoofed_headers_single_source_bucket(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_IPS", raising=False)
    with shared_rate_limiter(rate_limit_store):
        for i in range(2):
            response = _login(
                password="wrong",
                headers={"X-Forwarded-For": f"203.0.113.{i}"},
            )
            assert response.status_code == 401
        blocked = _login(
            password="wrong",
            headers={"X-Forwarded-For": "203.0.113.99"},
        )
    assert blocked.status_code == 429
    source_keys = {
        key
        for key in rate_limit_store.rows
        if key == admin_auth.build_source_rate_limit_key("testclient")
    }
    assert len(source_keys) == 1


@pytest.mark.integration
def test_trusted_proxy_login_limiter_uses_chain_client_not_spoof(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rotating leftmost X-Forwarded-For values must not mint fresh source buckets."""
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    _trusted_env(monkeypatch)
    render_client = _client_behind_render()
    from tests.test_admin_auth import (
        _mock_claim_admin_login_flow,
        _mock_cleanup_stale_admin_login_flows,
        _mock_create_admin_login_flow,
        _parse_login_form,
        mock_db_connection,
    )

    def _post_login(headers: dict[str, str]) -> Any:
        with mock_db_connection():
            form = render_client.get("/admin/login")
            csrf_token, flow_cookie = _parse_login_form(form)
            cookies = flow_cookie
            return render_client.post(
                "/admin/login",
                data={
                    "username": "ghost",
                    "password": "wrong",
                    "csrf_token": csrf_token,
                },
                headers=headers,
                cookies=cookies,
            )

    with (
        shared_rate_limiter(rate_limit_store),
        patch(
            "app.admin_auth.db.create_admin_login_flow",
            side_effect=_mock_create_admin_login_flow,
        ),
        patch(
            "app.admin_auth.db.cleanup_stale_admin_login_flows",
            side_effect=_mock_cleanup_stale_admin_login_flows,
        ),
        patch(
            "app.admin_auth.db.claim_admin_login_flow",
            side_effect=_mock_claim_admin_login_flow,
        ),
    ):
        same_real = "198.51.100.5"
        assert (
            _post_login({"X-Forwarded-For": f"203.0.113.1, {same_real}"}).status_code
            == 401
        )
        assert (
            _post_login({"X-Forwarded-For": f"203.0.113.2, {same_real}"}).status_code
            == 401
        )
        blocked = _post_login({"X-Forwarded-For": f"203.0.113.3, {same_real}"})
        assert blocked.status_code == 429

    expected_key = admin_auth.build_source_rate_limit_key(same_real)
    assert expected_key in rate_limit_store.rows
    assert len([k for k in rate_limit_store.rows if k.startswith("src") or len(k) == 64]) >= 1


def test_render_yaml_proxy_settings_consistent() -> None:
    text = RENDER_YAML.read_text(encoding="utf-8")
    start_match = re.search(
        r"startCommand:\s*>-\s*\n\s*uvicorn app\.main:app.*?--forwarded-allow-ips\s+([^\n]+)",
        text,
        re.DOTALL,
    )
    assert start_match is not None
    forwarded = start_match.group(1).strip()
    assert forwarded == "127.0.0.1,::1"
    assert "10.0.0.0/8" not in forwarded

    assert 'key: ADMIN_TRUSTED_PROXY_IPS\n        value: "10.0.0.0/8,127.0.0.1,::1"' in text
    assert 'key: ADMIN_TRUST_CLOUDFLARE_EDGE\n        value: "true"' in text
    assert "173.245.48.0/20" in text


def test_admin_auth_doc_documents_trust_model() -> None:
    text = ADMIN_AUTH_DOC.read_text(encoding="utf-8")
    assert "ADMIN_TRUSTED_PROXY_IPS" in text
    assert "right-to-left" in text
    assert "Rollback if proxy trust is misconfigured" in text
    assert "ADMIN_TRUST_PROXY_HEADERS" not in text


def test_telemetry_and_logs_exclude_raw_forwarding_data(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    _trusted_env(monkeypatch)
    settings = get_settings()
    request = _request_with_client("198.51.100.10")
    _append_header(request, "x-forwarded-for", "203.0.113.99")
    resolve_admin_login_client_source(request, settings)
    combined = caplog.text
    assert "203.0.113.99" not in combined
    assert "198.51.100.10" not in combined
    reject_records = [
        record
        for record in caplog.records
        if record.getMessage()
        == "Admin login source resolution rejected forwarding data"
    ]
    assert reject_records
    assert (
        reject_records[0].__dict__.get("reject_reason")
        == "untrusted_peer_with_forwarding"
    )


def test_limiter_rows_store_digests_not_raw_sources(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "1")
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_IPS", raising=False)
    with shared_rate_limiter(rate_limit_store):
        _login(
            password="wrong",
            headers={"X-Forwarded-For": "203.0.113.50"},
        )
    for key in rate_limit_store.rows:
        assert "203.0.113" not in key
        assert len(key) == 64


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.mark.integration
def test_uvicorn_forwarded_allow_ips_matches_deployment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise uvicorn with the same --forwarded-allow-ips boundary as render.yaml."""
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", "127.0.0.1/32")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    port = _pick_free_port()
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        forwarded_allow_ips="127.0.0.1,::1",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "uvicorn failed to start for proxy-trust integration test"

    async def _probe() -> None:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as http_client:
            health = await http_client.get("/health")
            assert health.status_code == 200
            assert health.json()["status"] == "ok"

    asyncio.run(_probe())
    server.should_exit = True
    thread.join(timeout=5)
