"""Tests for verified-hop admin login client source resolution (#239)."""

from __future__ import annotations

import logging
import socket
import threading
import time
from typing import Any
from unittest.mock import patch

import httpx
import pytest
import uvicorn
from fastapi import Request
from fastapi.testclient import TestClient

from app import admin_auth
from app.admin_client_source import (
    TRUST_MODEL_VERSION,
    client_ip,
    normalize_client_address,
    reset_client_source_telemetry_for_tests,
    resolve_admin_login_client_source,
    trust_model_summary,
)
from app.config import get_settings
from app.main import app

RENDER_PROXY = "10.0.0.1"
CLOUDFLARE_EGRESS = "173.245.48.1"
REAL_CLIENT = "198.51.100.20"
SPOOFED_CLIENT = "203.0.113.99"
OTHER_CLIENT = "203.0.113.88"


def _request(
    *,
    peer: str,
    headers: dict[str, str] | None = None,
) -> Request:
    header_list = [
        (name.lower().encode("ascii"), value.encode("ascii"))
        for name, value in (headers or {}).items()
    ]
    scope: dict[str, Any] = {
        "type": "http",
        "headers": header_list,
        "client": (peer, 12345),
        "method": "POST",
        "path": "/admin/login",
    }
    return Request(scope)


@pytest.fixture(autouse=True)
def _reset_telemetry() -> None:
    reset_client_source_telemetry_for_tests()


@pytest.mark.unit
def test_normalize_client_address_formats() -> None:
    assert normalize_client_address("203.0.113.1") == "203.0.113.1"
    assert normalize_client_address("203.0.113.1:443") == "203.0.113.1"
    assert normalize_client_address("[2001:db8::1]") == "2001:db8::1"
    assert normalize_client_address("[2001:db8::1]:443") == "2001:db8::1"
    assert normalize_client_address("::ffff:203.0.113.5") == "203.0.113.5"
    assert normalize_client_address(" 203.0.113.1 ") == "203.0.113.1"
    assert normalize_client_address("") is None
    assert normalize_client_address("not-an-ip") is None
    assert normalize_client_address("203.0.113.1:not-a-port") is None


@pytest.mark.unit
def test_direct_spoof_ignored_without_trusted_peer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    settings = get_settings()

    single = _request(peer="198.51.100.10", headers={"X-Forwarded-For": SPOOFED_CLIENT})
    multi = _request(
        peer="198.51.100.10",
        headers={"X-Forwarded-For": f"{SPOOFED_CLIENT}, {OTHER_CLIENT}"},
    )

    assert resolve_admin_login_client_source(single, settings).source == "198.51.100.10"
    assert resolve_admin_login_client_source(multi, settings).source == "198.51.100.10"
    assert resolve_admin_login_client_source(single, settings).path == "direct_peer"


@pytest.mark.unit
def test_direct_spoof_ignored_when_peer_not_trusted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    settings = get_settings()
    request = _request(
        peer="198.51.100.10",
        headers={
            "X-Forwarded-For": f"{SPOOFED_CLIENT}, {REAL_CLIENT}",
            "CF-Connecting-IP": SPOOFED_CLIENT,
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "198.51.100.10"
    assert resolution.path == "untrusted_peer"


@pytest.mark.unit
def test_cloudflare_append_selects_real_client_not_leftmost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    settings = get_settings()
    request = _request(
        peer=RENDER_PROXY,
        headers={
            "X-Forwarded-For": f"{SPOOFED_CLIENT}, {REAL_CLIENT}, {CLOUDFLARE_EGRESS}",
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
    assert resolution.path == "trusted_xff"


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    settings = get_settings()
    request = _request(
        peer=RENDER_PROXY,
        headers={"X-Forwarded-For": f"{REAL_CLIENT}, {CLOUDFLARE_EGRESS}, {RENDER_PROXY}"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
    assert resolution.path == "trusted_xff"


@pytest.mark.unit
def test_partial_trust_fails_closed_behind_untrusted_intermediary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    settings = get_settings()
    untrusted_intermediary = "203.0.113.50"
    request = _request(
        peer=RENDER_PROXY,
        headers={
            "X-Forwarded-For": f"{REAL_CLIENT}, {untrusted_intermediary}, {RENDER_PROXY}",
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == untrusted_intermediary
    assert resolution.path == "trusted_xff"


@pytest.mark.unit
def test_direct_render_origin_ignores_spoofed_cloudflare_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    settings = get_settings()
    request = _request(
        peer=RENDER_PROXY,
        headers={
            "CF-Connecting-IP": SPOOFED_CLIENT,
            "X-Forwarded-For": f"{SPOOFED_CLIENT}, {REAL_CLIENT}",
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
    assert resolution.path == "trusted_xff"


@pytest.mark.unit
def test_cf_connecting_ip_used_only_after_cloudflare_edge_verified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    settings = get_settings()
    request = _request(
        peer=RENDER_PROXY,
        headers={
            "CF-Connecting-IP": REAL_CLIENT,
            "X-Forwarded-For": CLOUDFLARE_EGRESS,
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
    assert resolution.path == "trusted_cf_connecting_ip"


@pytest.mark.unit
def test_header_precedence_prefers_xff_over_forwarded_and_cf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    settings = get_settings()
    request = _request(
        peer=RENDER_PROXY,
        headers={
            "X-Forwarded-For": f"{REAL_CLIENT}, {CLOUDFLARE_EGRESS}",
            "Forwarded": f'for="{OTHER_CLIENT}";proto=https',
            "CF-Connecting-IP": OTHER_CLIENT,
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
    assert resolution.path == "trusted_xff"


@pytest.mark.unit
def test_forwarded_header_used_when_xff_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    settings = get_settings()
    request = _request(
        peer=RENDER_PROXY,
        headers={
            "Forwarded": (
                f'for="{SPOOFED_CLIENT}";proto=https, '
                f'for="{REAL_CLIENT}";proto=https, '
                f'for="{CLOUDFLARE_EGRESS}";proto=https'
            ),
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
    assert resolution.path == "trusted_forwarded"


@pytest.mark.unit
def test_invalid_and_overlong_forwarding_data_is_conservative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    settings = get_settings()

    empty_elements = _request(
        peer=RENDER_PROXY,
        headers={"X-Forwarded-For": f"{REAL_CLIENT}, , {CLOUDFLARE_EGRESS}"},
    )
    assert resolve_admin_login_client_source(empty_elements, settings).path == "invalid_forwarding"

    overlong = _request(
        peer=RENDER_PROXY,
        headers={"X-Forwarded-For": ", ".join(f"10.0.{i}.1" for i in range(40))},
    )
    assert resolve_admin_login_client_source(overlong, settings).path == "invalid_forwarding"


@pytest.mark.unit
def test_missing_peer_returns_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    settings = get_settings()
    scope: dict[str, Any] = {
        "type": "http",
        "headers": [],
        "client": None,
        "method": "POST",
        "path": "/admin/login",
    }
    resolution = resolve_admin_login_client_source(Request(scope), settings)
    assert resolution.source == "unknown"
    assert resolution.path == "missing_peer"


@pytest.mark.unit
def test_telemetry_logs_path_without_raw_addresses(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    settings = get_settings()
    caplog.set_level(logging.INFO)
    request = _request(peer="198.51.100.10", headers={"X-Forwarded-For": SPOOFED_CLIENT})

    with caplog.at_level(logging.INFO):
        resolve_admin_login_client_source(request, settings)
        time.sleep(0.01)
        resolve_admin_login_client_source(request, settings)

    assert any(
        "client_source_path" in record.__dict__ and record.__dict__["client_source_path"]
        == "untrusted_forwarding_ignored"
        for record in caplog.records
    )
    assert SPOOFED_CLIENT not in caplog.text
    assert "x-forwarded-for" not in caplog.text.lower()


@pytest.mark.unit
def test_trust_model_summary_for_health() -> None:
    summary = trust_model_summary(get_settings())
    assert summary["admin_client_source_trust"] == TRUST_MODEL_VERSION
    assert summary["admin_proxy_headers_enabled"] is False


@pytest.mark.unit
def test_limiter_keys_never_store_raw_source_material() -> None:
    source_key = admin_auth.build_source_rate_limit_key(REAL_CLIENT)
    assert REAL_CLIENT not in source_key
    assert len(source_key) == 64


@pytest.mark.unit
def test_health_endpoint_includes_trust_metadata() -> None:
    response = TestClient(app).get("/health")
    payload = response.json()
    assert payload["admin_client_source_trust"] == TRUST_MODEL_VERSION
    assert "admin_trust_proxy_headers" in payload
    assert "uvicorn_forwarded_allow_ips" in payload


@pytest.mark.integration
def test_rotating_spoofed_headers_do_not_create_new_limiter_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_admin_auth import (  # noqa: PLC0415
        TEST_HASH,
        FakeRateLimitStore,
        shared_rate_limiter,
        mock_db_connection,
        _login,
    )

    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", "operator")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "test-session-secret-32chars-minimum")
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", "testclient")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "3")
    admin_auth.reset_login_rate_limiter()
    store = FakeRateLimitStore()

    with shared_rate_limiter(store):
        for index in range(3):
            headers = {
                "X-Forwarded-For": f"203.0.113.{index}, {REAL_CLIENT}, {CLOUDFLARE_EGRESS}",
            }
            with mock_db_connection():
                response = _login(username="ghost", password="wrong", headers=headers)
            assert response.status_code == 401

        blocked_headers = {
            "X-Forwarded-For": f"{OTHER_CLIENT}, {REAL_CLIENT}, {CLOUDFLARE_EGRESS}",
        }
        with mock_db_connection():
            blocked = _login(username="ghost", password="wrong", headers=blocked_headers)
        assert blocked.status_code == 429
        assert len(store.rows) == 1


def _reserve_ephemeral_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.mark.integration
def test_uvicorn_integration_respects_verified_hop_trust(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("UVICORN_FORWARDED_ALLOW_IPS", "127.0.0.1")
    reset_client_source_telemetry_for_tests()

    with patch("app.main.db.init_db"):
        port = _reserve_ephemeral_port()
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            proxy_headers=False,
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
            thread.join(timeout=2)
            pytest.fail("uvicorn did not start")

        try:
            with httpx.Client(
                base_url=f"http://127.0.0.1:{port}",
                headers={
                    "X-Forwarded-For": f"{SPOOFED_CLIENT}, {REAL_CLIENT}, {CLOUDFLARE_EGRESS}",
                },
                timeout=2.0,
            ) as http_client:
                health = http_client.get("/health")
                assert health.status_code == 200
                assert health.json()["admin_client_source_trust"] == TRUST_MODEL_VERSION

                settings = get_settings()
                scope_request = _request(
                    peer="127.0.0.1",
                    headers={
                        "X-Forwarded-For": f"{SPOOFED_CLIENT}, {REAL_CLIENT}, {CLOUDFLARE_EGRESS}",
                    },
                )
                assert client_ip(scope_request, settings) == REAL_CLIENT
        finally:
            server.should_exit = True
            thread.join(timeout=3)
