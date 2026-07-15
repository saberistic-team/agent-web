"""Tests for trusted-proxy admin login source resolution (#239)."""

from __future__ import annotations

import logging
import socket
import threading
import time
from pathlib import Path
from typing import Generator

import httpx
import pytest
import uvicorn
from argon2 import PasswordHasher
from fastapi import Request

from app import admin_auth
from app.config import get_settings
from app.trusted_proxy import (
    SOURCE_UNKNOWN,
    SourceResolutionPath,
    normalize_ip_address,
    parse_cidr_list,
    resolve_admin_login_client_source,
)
from app.main import app

REPO_ROOT = Path(__file__).resolve().parent.parent
RENDER_TRUSTED = "10.0.0.0/8,172.16.0.0/12"
TEST_CF_EDGE = "172.64.0.0/13"
TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "test-session-secret-32chars-minimum"


def _trusted_networks() -> tuple:
    return parse_cidr_list(RENDER_TRUSTED)


def _cloudflare_networks() -> tuple:
    return parse_cidr_list(TEST_CF_EDGE)


def _resolve(
    *,
    peer_host: str | None,
    headers: dict[str, str] | None = None,
) -> str:
    result = resolve_admin_login_client_source(
        peer_host=peer_host,
        headers=headers or {},
        trusted_proxy_networks=_trusted_networks(),
        cloudflare_edge_networks=_cloudflare_networks(),
    )
    return result.source


def _request(peer_host: str, headers: dict[str, str] | None = None) -> Request:
    header_list = [
        (key.lower().encode("ascii"), value.encode("ascii"))
        for key, value in (headers or {}).items()
    ]
    scope = {
        "type": "http",
        "headers": header_list,
        "client": (peer_host, 12345),
        "method": "POST",
        "path": "/admin/login",
    }
    return Request(scope)


@pytest.fixture
def proxy_admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "3")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", f"127.0.0.0/8,{RENDER_TRUSTED}")
    monkeypatch.setenv("ADMIN_CLOUDFLARE_EDGE_CIDRS", TEST_CF_EDGE)
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    admin_auth.reset_login_rate_limiter()


@pytest.fixture
def uvicorn_server(proxy_admin_env: None, monkeypatch: pytest.MonkeyPatch) -> Generator[str, None, None]:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        proxy_headers=False,
        forwarded_allow_ips="127.0.0.0/8,10.0.0.0/8,172.16.0.0/12",
        lifespan="off",
    )
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 5
    origin = f"http://127.0.0.1:{port}"
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{origin}/health", timeout=0.5)
            if response.status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.05)
    else:
        server.should_exit = True
        thread.join(timeout=2)
        raise RuntimeError("uvicorn server failed to start")

    yield origin

    server.should_exit = True
    thread.join(timeout=2)


@pytest.mark.unit
def test_direct_spoof_single_and_multi_value_xff_ignored() -> None:
    peer = "198.51.100.10"
    assert _resolve(peer_host=peer, headers={"X-Forwarded-For": "203.0.113.99"}) == peer
    assert (
        _resolve(
            peer_host=peer,
            headers={"X-Forwarded-For": "203.0.113.1, 203.0.113.2, 203.0.113.3"},
        )
        == peer
    )


@pytest.mark.unit
def test_cloudflare_append_selects_connecting_address_not_leftmost() -> None:
    assert (
        _resolve(
            peer_host="10.0.0.5",
            headers={"X-Forwarded-For": "203.0.113.9, 203.0.113.50"},
        )
        == "203.0.113.50"
    )


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client() -> None:
    assert (
        _resolve(
            peer_host="10.0.0.1",
            headers={"X-Forwarded-For": "203.0.113.77, 10.0.0.1"},
        )
        == "203.0.113.77"
    )


@pytest.mark.unit
def test_partial_trust_untrusted_intermediary_fails_closed() -> None:
    peer = "198.51.100.44"
    assert (
        _resolve(
            peer_host=peer,
            headers={
                "X-Forwarded-For": "203.0.113.10, 10.0.0.1",
                "CF-Connecting-IP": "203.0.113.99",
            },
        )
        == peer
    )


@pytest.mark.unit
def test_direct_render_origin_ignores_spoofed_cloudflare_header() -> None:
    peer = "198.51.100.55"
    assert (
        _resolve(
            peer_host=peer,
            headers={
                "CF-Connecting-IP": "203.0.113.88",
                "X-Forwarded-For": "203.0.113.88",
            },
        )
        == peer
    )


@pytest.mark.unit
def test_header_precedence_xff_before_forwarded_and_cf_without_proof() -> None:
    result = resolve_admin_login_client_source(
        peer_host="10.0.0.2",
        headers={
            "X-Forwarded-For": "203.0.113.5, 10.0.0.2",
            "Forwarded": 'for=203.0.113.9;proto=https, for="10.0.0.2"',
            "CF-Connecting-IP": "203.0.113.1",
        },
        trusted_proxy_networks=_trusted_networks(),
        cloudflare_edge_networks=_cloudflare_networks(),
    )
    assert result.source == "203.0.113.5"
    assert result.path is SourceResolutionPath.XFF_RIGHT_TO_LEFT


@pytest.mark.unit
def test_cf_connecting_ip_used_when_cloudflare_hop_proven() -> None:
    result = resolve_admin_login_client_source(
        peer_host="10.0.0.3",
        headers={
            "X-Forwarded-For": "203.0.113.60, 172.64.15.1, 10.0.0.3",
            "CF-Connecting-IP": "203.0.113.60",
        },
        trusted_proxy_networks=_trusted_networks(),
        cloudflare_edge_networks=_cloudflare_networks(),
    )
    assert result.source == "203.0.113.60"
    assert result.path is SourceResolutionPath.CF_CONNECTING_IP


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("203.0.113.1", "203.0.113.1"),
        ("203.0.113.1:443", "203.0.113.1"),
        ("2001:db8::1", "2001:db8::1"),
        ("[2001:db8::1]:443", "2001:db8::1"),
        ("::ffff:203.0.113.5", "203.0.113.5"),
        ("  203.0.113.2  ", "203.0.113.2"),
        ("", None),
        ("not-an-ip", None),
    ],
)
def test_normalize_ip_address_formats(raw: str, expected: str | None) -> None:
    assert normalize_ip_address(raw) == expected


@pytest.mark.unit
def test_empty_and_invalid_xff_elements_are_skipped_deterministically() -> None:
    assert (
        _resolve(
            peer_host="10.0.0.4",
            headers={"X-Forwarded-For": " , bad, 203.0.113.8 , , 10.0.0.4"},
        )
        == "203.0.113.8"
    )


@pytest.mark.unit
def test_overlong_forwarding_chain_is_bounded() -> None:
    hops = ", ".join(f"203.0.113.{index}" for index in range(40))
    source = _resolve(peer_host="10.0.0.6", headers={"X-Forwarded-For": hops})
    assert source.startswith("203.0.113.")


@pytest.mark.unit
def test_trusted_peer_without_usable_forwarding_uses_unknown() -> None:
    result = resolve_admin_login_client_source(
        peer_host="10.0.0.7",
        headers={},
        trusted_proxy_networks=_trusted_networks(),
        cloudflare_edge_networks=_cloudflare_networks(),
    )
    assert result.source == SOURCE_UNKNOWN
    assert result.path is SourceResolutionPath.TRUSTED_NO_FORWARDING


@pytest.mark.unit
def test_client_ip_integration_with_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", RENDER_TRUSTED)
    monkeypatch.setenv("ADMIN_CLOUDFLARE_EDGE_CIDRS", TEST_CF_EDGE)
    settings = get_settings()
    request = _request(
        "10.0.0.8",
        {"X-Forwarded-For": "203.0.113.44, 10.0.0.8"},
    )
    assert admin_auth.client_ip(request, settings) == "203.0.113.44"


@pytest.mark.unit
@pytest.mark.integration
def test_rotating_spoofed_headers_do_not_create_new_limiter_sources(
    proxy_admin_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_admin_auth import FakeRateLimitStore, mock_db_connection, shared_rate_limiter, _login

    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "3")
    store = FakeRateLimitStore()
    with shared_rate_limiter(store), mock_db_connection():
        for index in range(5):
            response = _login(
                username="ghost",
                password="wrong",
                headers={"X-Forwarded-For": f"203.0.113.{index}"},
            )
            if index < 3:
                assert response.status_code == 401
            else:
                assert response.status_code == 429

        blocked = _login(
            username="ghost",
            password="wrong",
            headers={"X-Forwarded-For": "203.0.113.99"},
        )
        assert blocked.status_code == 429

    assert len(store.rows) == 1
    assert admin_auth.build_source_rate_limit_key("testclient") in store.rows


@pytest.mark.unit
def test_render_yaml_proxy_trust_settings_are_consistent() -> None:
    render_yaml = (REPO_ROOT / "render.yaml").read_text()
    docs = render_yaml.split("services:")
    service_block = docs[1]
    start_command = service_block.split("startCommand:", 1)[1].split("\n", 1)[0].strip()

    assert "--no-proxy-headers" in start_command
    assert "--forwarded-allow-ips" in start_command
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in render_yaml
    assert "ADMIN_CLOUDFLARE_EDGE_CIDRS" in render_yaml
    assert "10.0.0.0/8" in render_yaml
    assert "172.64.0.0/13" in render_yaml

    for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"):
        assert cidr in start_command


@pytest.mark.unit
def test_health_reports_admin_login_source_trust(
    proxy_admin_env: None,
) -> None:
    from fastapi.testclient import TestClient

    response = TestClient(app).get("/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["admin_login_source_trust"] == {
        "configured": True,
        "uvicorn_proxy_headers": False,
    }


@pytest.mark.unit
def test_source_resolution_telemetry_contains_no_raw_addresses(
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = get_settings()
    request = _request(
        "198.51.100.20",
        {"X-Forwarded-For": "203.0.113.70", "CF-Connecting-IP": "203.0.113.71"},
    )
    with caplog.at_level(logging.INFO, logger="app.trusted_proxy"):
        source = admin_auth.client_ip(request, settings)

    assert source == "198.51.100.20"
    assert caplog.records
    record = caplog.records[-1]
    assert record.__dict__.get("source_resolution_path") == "untrusted_headers_ignored"
    combined = caplog.text + str(record.__dict__)
    assert "203.0.113" not in combined


@pytest.mark.unit
@pytest.mark.integration
def test_trusted_peer_limiter_rotating_spoofed_xff_shares_one_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_admin_auth import FakeRateLimitStore, shared_rate_limiter

    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", RENDER_TRUSTED)
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "5")
    settings = get_settings()
    store = FakeRateLimitStore()
    real_client = "203.0.113.77"
    with shared_rate_limiter(store):
        for index in range(4):
            request = _request(
                "10.0.0.9",
                {"X-Forwarded-For": f"203.0.113.{index}, {real_client}"},
            )
            admission = admin_auth.try_admit_login_attempt(
                request,
                settings,
                username="ghost",
            )
            assert admission.admitted

    assert len(store.rows) == 1
    expected_key = admin_auth.build_source_rate_limit_key(real_client)
    assert expected_key in store.rows


@pytest.mark.unit
@pytest.mark.integration
def test_uvicorn_health_proxy_trust_matches_deployment(uvicorn_server: str) -> None:
    response = httpx.get(f"{uvicorn_server}/health", timeout=2.0)
    assert response.status_code == 200
    trust = response.json()["admin_login_source_trust"]
    assert trust["configured"] is True
    assert trust["uvicorn_proxy_headers"] is False
