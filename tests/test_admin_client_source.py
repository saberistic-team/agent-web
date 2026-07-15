"""Tests for trusted-proxy admin login client source resolution (#239)."""

from __future__ import annotations

pytest_plugins = ["tests.test_admin_auth"]

import json
import socket
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

from typing import TYPE_CHECKING

import httpx
import pytest
import uvicorn
from fastapi import Request
from fastapi.testclient import TestClient

from app import admin_auth
from app.admin_client_source import (
    SourceResolutionPath,
    normalize_ip,
    parse_cidr_list,
    peer_is_trusted,
    reset_source_resolution_telemetry,
    resolve_admin_login_client_source,
)
from app.config import get_settings
from app.main import app
from tests.test_admin_auth import (
    _extract_csrf_token,
    mock_db_connection,
    shared_rate_limiter,
)

if TYPE_CHECKING:
    from tests.test_admin_auth import FakeRateLimitStore

# Render-trusted peer used in integration scenarios.
TRUSTED_RENDER_PEER = "10.0.0.5"
# Example Cloudflare edge from published ranges.
CLOUDFLARE_EDGE = "104.16.0.10"
REAL_CLIENT = "203.0.113.77"
SPOOFED_CLIENT = "198.51.100.99"
OTHER_CLIENT = "203.0.113.88"


def _request_with_client(
    host: str,
    *,
    headers: dict[str, str] | None = None,
) -> Request:
    scope: dict[str, Any] = {
        "type": "http",
        "headers": [],
        "client": (host, 12345),
        "method": "POST",
        "path": "/admin/login",
    }
    if headers:
        scope["headers"] = [
            (key.lower().encode("ascii"), value.encode("ascii"))
            for key, value in headers.items()
        ]
    return Request(scope)


def _settings_with_trusted_proxy(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv(
        "ADMIN_TRUSTED_PROXY_CIDRS",
        "10.0.0.0/8,127.0.0.1",
    )
    return get_settings()


def _proxy_test_client(peer_host: str) -> TestClient:
    return TestClient(app, client=(peer_host, 50000))


@pytest.fixture(autouse=True)
def reset_telemetry() -> None:
    reset_source_resolution_telemetry()
    admin_auth.reset_login_rate_limiter()


@pytest.mark.unit
def test_parse_cidr_list_ignores_empty_tokens() -> None:
    assert parse_cidr_list("10.0.0.0/8, ,127.0.0.1") == ("10.0.0.0/8", "127.0.0.1")


@pytest.mark.unit
def test_normalize_ip_variants() -> None:
    assert normalize_ip("203.0.113.1") == "203.0.113.1"
    assert normalize_ip("203.0.113.1:443") == "203.0.113.1"
    assert normalize_ip("2001:db8::1") == "2001:db8::1"
    assert normalize_ip("::ffff:203.0.113.50") == "203.0.113.50"
    assert normalize_ip("  203.0.113.1  ") == "203.0.113.1"
    assert normalize_ip("not-an-ip") is None
    assert normalize_ip("") is None


@pytest.mark.unit
def test_peer_is_trusted_requires_configured_boundary() -> None:
    assert peer_is_trusted("10.0.0.5", ("10.0.0.0/8",))
    assert not peer_is_trusted("203.0.113.1", ("10.0.0.0/8",))
    assert not peer_is_trusted("10.0.0.5", ())


@pytest.mark.unit
def test_direct_spoof_single_and_multi_value_xff_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    settings = get_settings()
    direct_peer = "198.51.100.10"

    single = _request_with_client(
        direct_peer,
        headers={"X-Forwarded-For": SPOOFED_CLIENT},
    )
    multi = _request_with_client(
        direct_peer,
        headers={"X-Forwarded-For": f"{SPOOFED_CLIENT}, {REAL_CLIENT}"},
    )

    assert resolve_admin_login_client_source(single, settings).source == direct_peer
    assert resolve_admin_login_client_source(multi, settings).source == direct_peer
    assert (
        resolve_admin_login_client_source(single, settings).path
        == SourceResolutionPath.DIRECT_PEER
    )


@pytest.mark.unit
def test_cloudflare_append_selects_real_client_not_leftmost_spoof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_trusted_proxy(monkeypatch)
    request = _request_with_client(
        TRUSTED_RENDER_PEER,
        headers={
            "X-Forwarded-For": f"{SPOOFED_CLIENT}, {REAL_CLIENT}",
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
    assert resolution.path == SourceResolutionPath.TRUSTED_X_FORWARDED_FOR


@pytest.mark.unit
def test_trusted_chain_through_cloudflare_and_render(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_trusted_proxy(monkeypatch)
    request = _request_with_client(
        TRUSTED_RENDER_PEER,
        headers={
            "X-Forwarded-For": f"{REAL_CLIENT}, {CLOUDFLARE_EDGE}",
            "CF-Connecting-IP": REAL_CLIENT,
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
    assert resolution.path == SourceResolutionPath.TRUSTED_CF_CONNECTING_IP


@pytest.mark.unit
def test_partial_trust_untrusted_intermediary_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    settings = get_settings()
    request = _request_with_client(
        "203.0.113.5",
        headers={"X-Forwarded-For": f"{REAL_CLIENT}, {TRUSTED_RENDER_PEER}"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.5"
    assert resolution.path == SourceResolutionPath.DIRECT_PEER


@pytest.mark.unit
def test_direct_render_origin_ignores_spoofed_cf_connecting_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_trusted_proxy(monkeypatch)
    request = _request_with_client(
        TRUSTED_RENDER_PEER,
        headers={
            "CF-Connecting-IP": SPOOFED_CLIENT,
            "X-Forwarded-For": REAL_CLIENT,
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
    assert resolution.path == SourceResolutionPath.TRUSTED_X_FORWARDED_FOR


@pytest.mark.unit
def test_header_precedence_cf_requires_cloudflare_hop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_trusted_proxy(monkeypatch)
    request = _request_with_client(
        TRUSTED_RENDER_PEER,
        headers={
            "CF-Connecting-IP": SPOOFED_CLIENT,
            "X-Forwarded-For": REAL_CLIENT,
            "Forwarded": f'for="{OTHER_CLIENT}"',
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
    assert resolution.path == SourceResolutionPath.TRUSTED_X_FORWARDED_FOR


@pytest.mark.unit
def test_forwarded_header_used_when_xff_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_trusted_proxy(monkeypatch)
    request = _request_with_client(
        TRUSTED_RENDER_PEER,
        headers={"Forwarded": f'for="{REAL_CLIENT}", for="{CLOUDFLARE_EDGE}"'},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
    assert resolution.path == SourceResolutionPath.TRUSTED_FORWARDED


@pytest.mark.unit
def test_address_format_edge_cases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_trusted_proxy(monkeypatch)

    ipv6 = _request_with_client(
        TRUSTED_RENDER_PEER,
        headers={"X-Forwarded-For": "2001:db8::9, 10.0.0.1"},
    )
    assert resolve_admin_login_client_source(ipv6, settings).source == "2001:db8::9"

    mapped = _request_with_client(
        TRUSTED_RENDER_PEER,
        headers={"X-Forwarded-For": "::ffff:203.0.113.60"},
    )
    assert resolve_admin_login_client_source(mapped, settings).source == "203.0.113.60"

    whitespace = _request_with_client(
        TRUSTED_RENDER_PEER,
        headers={"X-Forwarded-For": f"  {REAL_CLIENT}  , {CLOUDFLARE_EDGE} "},
    )
    assert resolve_admin_login_client_source(whitespace, settings).source == REAL_CLIENT

    empty_elements = _request_with_client(
        TRUSTED_RENDER_PEER,
        headers={"X-Forwarded-For": f",,{REAL_CLIENT},,"},
    )
    assert resolve_admin_login_client_source(empty_elements, settings).source == REAL_CLIENT

    invalid = _request_with_client(
        TRUSTED_RENDER_PEER,
        headers={"X-Forwarded-For": "not-valid, also-bad"},
    )
    assert (
        resolve_admin_login_client_source(invalid, settings).path
        == SourceResolutionPath.INVALID_FORWARDING_IGNORED
    )

    overlong = _request_with_client(
        TRUSTED_RENDER_PEER,
        headers={"X-Forwarded-For": ", ".join(["203.0.113.1"] * 40)},
    )
    assert (
        resolve_admin_login_client_source(overlong, settings).path
        == SourceResolutionPath.INVALID_FORWARDING_IGNORED
    )


@pytest.mark.unit
def test_missing_peer_resolves_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    settings = get_settings()
    request = Request({"type": "http", "headers": [], "method": "GET", "path": "/"})
    assert resolve_admin_login_client_source(request, settings).source == "unknown"


@pytest.mark.unit
@pytest.mark.integration
def test_rotating_spoofed_headers_share_one_limiter_bucket(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", "10.0.0.0/8")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            proxy_client = _proxy_test_client(TRUSTED_RENDER_PEER)
            form = proxy_client.get("/admin/login")
            csrf_token = _extract_csrf_token(form.text)
            cookies = {admin_auth.LOGIN_FLOW_COOKIE_NAME: form.cookies[admin_auth.LOGIN_FLOW_COOKIE_NAME]}
            for index in range(2):
                response = proxy_client.post(
                    "/admin/login",
                    data={
                        "username": "ghost",
                        "password": "wrong",
                        "csrf_token": csrf_token,
                    },
                    cookies=cookies,
                    headers={"X-Forwarded-For": f"198.51.100.{index}, {REAL_CLIENT}"},
                )
                assert response.status_code == 401
                csrf_token = _extract_csrf_token(response.text)
                flow_cookie = response.cookies.get(admin_auth.LOGIN_FLOW_COOKIE_NAME)
                if flow_cookie:
                    cookies[admin_auth.LOGIN_FLOW_COOKIE_NAME] = flow_cookie

            blocked = proxy_client.post(
                "/admin/login",
                data={
                    "username": "ghost",
                    "password": "wrong",
                    "csrf_token": csrf_token,
                },
                cookies=cookies,
                headers={"X-Forwarded-For": f"198.51.100.99, {REAL_CLIENT}"},
            )
            assert blocked.status_code == 429

    assert len(rate_limit_store.rows) == 1


@pytest.mark.unit
@pytest.mark.integration
def test_trusted_proxy_limiter_uses_parsed_client_not_spoof(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", "10.0.0.0/8")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            proxy_client = _proxy_test_client(TRUSTED_RENDER_PEER)
            headers = {"X-Forwarded-For": f"{SPOOFED_CLIENT}, {REAL_CLIENT}"}
            form = proxy_client.get("/admin/login")
            csrf_token = _extract_csrf_token(form.text)
            cookies = {admin_auth.LOGIN_FLOW_COOKIE_NAME: form.cookies[admin_auth.LOGIN_FLOW_COOKIE_NAME]}
            for _ in range(2):
                response = proxy_client.post(
                    "/admin/login",
                    data={
                        "username": "ghost",
                        "password": "wrong",
                        "csrf_token": csrf_token,
                    },
                    cookies=cookies,
                    headers=headers,
                )
                assert response.status_code == 401
                csrf_token = _extract_csrf_token(response.text)
                flow_cookie = response.cookies.get(admin_auth.LOGIN_FLOW_COOKIE_NAME)
                if flow_cookie:
                    cookies[admin_auth.LOGIN_FLOW_COOKIE_NAME] = flow_cookie

            blocked = proxy_client.post(
                "/admin/login",
                data={
                    "username": "ghost",
                    "password": "wrong",
                    "csrf_token": csrf_token,
                },
                cookies=cookies,
                headers={"X-Forwarded-For": f"other-spoof, {REAL_CLIENT}"},
            )
            assert blocked.status_code == 429


@pytest.mark.unit
def test_telemetry_contains_no_raw_forwarding_data(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = _settings_with_trusted_proxy(monkeypatch)
    request = _request_with_client(
        "203.0.113.5",
        headers={"X-Forwarded-For": SPOOFED_CLIENT},
    )
    with caplog.at_level("INFO"):
        resolve_admin_login_client_source(request, settings)
    for record in caplog.records:
        message = record.getMessage()
        assert SPOOFED_CLIENT not in message
        assert "x-forwarded-for" not in message.lower()
        if hasattr(record, "source_resolution_path"):
            assert record.source_resolution_path  # type: ignore[attr-defined]


@pytest.mark.unit
def test_limiter_rows_store_digests_not_raw_sources(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", "10.0.0.0/8")
    proxy_client = _proxy_test_client(TRUSTED_RENDER_PEER)
    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            form = proxy_client.get("/admin/login")
            csrf_token = _extract_csrf_token(form.text)
            cookies = {admin_auth.LOGIN_FLOW_COOKIE_NAME: form.cookies[admin_auth.LOGIN_FLOW_COOKIE_NAME]}
            proxy_client.post(
                "/admin/login",
                data={
                    "username": "ghost",
                    "password": "wrong",
                    "csrf_token": csrf_token,
                },
                cookies=cookies,
                headers={"X-Forwarded-For": REAL_CLIENT},
            )
    for key in rate_limit_store.rows:
        assert REAL_CLIENT not in key
        assert len(key) == 64


@pytest.mark.unit
def test_render_yaml_proxy_settings_are_consistent() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    render_yaml = (repo_root / "render.yaml").read_text(encoding="utf-8")
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in render_yaml
    assert "--proxy-headers" in render_yaml
    assert "--forwarded-allow-ips" in render_yaml
    assert "10.0.0.0/8" in render_yaml


@pytest.mark.unit
def test_health_reports_admin_client_source_when_boundary_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", "10.0.0.0/8")
    client = TestClient(app)
    payload = client.get("/health").json()
    assert payload["admin_client_source"]["trusted_proxy_boundary"] is True
    assert payload["admin_client_source"]["uvicorn_proxy_headers"] is True


@pytest.mark.unit
def test_legacy_admin_trust_proxy_headers_maps_to_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    settings = get_settings()
    assert "10.0.0.0/8" in settings.admin_trusted_proxy_cidrs


def _get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.mark.integration
def test_uvicorn_proxy_headers_integration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the same uvicorn proxy-header flags declared in render.yaml."""
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", "127.0.0.1")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    port = _get_free_port()
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)
    else:
        server.should_exit = True
        pytest.fail("uvicorn did not start")

    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}") as http_client:
            health = http_client.get("/health")
            assert health.status_code == 200
            payload = health.json()
            assert payload["admin_client_source"]["trusted_proxy_boundary"] is True

            settings = get_settings()
            request = _request_with_client(
                "127.0.0.1",
                headers={"X-Forwarded-For": f"{SPOOFED_CLIENT}, {REAL_CLIENT}"},
            )
            resolution = resolve_admin_login_client_source(request, settings)
            assert resolution.source == REAL_CLIENT
    finally:
        server.should_exit = True
        thread.join(timeout=3)
