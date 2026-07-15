"""Tests for trusted-hop admin login client source resolution (#239)."""

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
from fastapi import Request

from app import admin_auth
from app.admin_client_source import (
    DEFAULT_RENDER_FORWARDED_ALLOW_IPS,
    DEFAULT_RENDER_TRUSTED_PROXY_CIDRS,
    ClientSourceResolution,
    normalize_client_address,
    reset_client_source_telemetry_for_tests,
    resolve_admin_login_client_source,
)
from app.config import Settings, get_settings

RENDER_TRUSTED = ",".join(DEFAULT_RENDER_TRUSTED_PROXY_CIDRS)
RENDER_PROXY = "10.0.0.1"
REAL_CLIENT = "203.0.113.50"
ATTACKER_SPOOF = "198.51.100.99"
REAL_CONNECTOR = "198.51.100.10"


def _settings(
    monkeypatch: pytest.MonkeyPatch,
    *,
    trusted_cidrs: str = "",
    legacy_trust: bool = False,
) -> Settings:
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    if trusted_cidrs:
        monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", trusted_cidrs)
    elif legacy_trust:
        monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    reset_client_source_telemetry_for_tests()
    admin_auth.reset_login_rate_limiter()
    return get_settings()


def _request(
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


@pytest.fixture(autouse=True)
def _reset_telemetry() -> None:
    reset_client_source_telemetry_for_tests()
    admin_auth.reset_login_rate_limiter()


@pytest.mark.unit
def test_normalize_ipv4_ipv6_mapped_and_ports() -> None:
    assert normalize_client_address("203.0.113.1") == "203.0.113.1"
    assert normalize_client_address("203.0.113.1:443") == "203.0.113.1"
    assert normalize_client_address("2001:db8::1") == "2001:db8::1"
    assert normalize_client_address("[2001:db8::1]:443") == "2001:db8::1"
    assert normalize_client_address("::ffff:203.0.113.1") == "203.0.113.1"
    assert normalize_client_address(" 203.0.113.1 ") == "203.0.113.1"
    assert normalize_client_address("") is None
    assert normalize_client_address("not-an-ip") == "not-an-ip"


@pytest.mark.unit
def test_direct_spoof_single_and_multi_value_xff_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, trusted_cidrs=RENDER_TRUSTED)
    single = resolve_admin_login_client_source(
        _request(REAL_CONNECTOR, {"X-Forwarded-For": ATTACKER_SPOOF}),
        settings,
    )
    multi = resolve_admin_login_client_source(
        _request(
            REAL_CONNECTOR,
            {"X-Forwarded-For": f"{ATTACKER_SPOOF}, {REAL_CLIENT}, {RENDER_PROXY}"},
        ),
        settings,
    )
    assert single == ClientSourceResolution(REAL_CONNECTOR, "direct_peer")
    assert multi == ClientSourceResolution(REAL_CONNECTOR, "direct_peer")


@pytest.mark.unit
def test_cloudflare_append_behavior_ignores_leftmost_spoof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, trusted_cidrs=RENDER_TRUSTED)
    resolution = resolve_admin_login_client_source(
        _request(
            RENDER_PROXY,
            {"X-Forwarded-For": f"{ATTACKER_SPOOF}, {REAL_CONNECTOR}"},
        ),
        settings,
    )
    assert resolution.address == REAL_CONNECTOR
    assert resolution.path == "trusted_chain"


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, trusted_cidrs=RENDER_TRUSTED)
    resolution = resolve_admin_login_client_source(
        _request(RENDER_PROXY, {"X-Forwarded-For": REAL_CLIENT}),
        settings,
    )
    assert resolution.address == REAL_CLIENT
    assert resolution.path == "trusted_chain"


@pytest.mark.unit
def test_partial_trust_untrusted_intermediary_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, trusted_cidrs=RENDER_TRUSTED)
    resolution = resolve_admin_login_client_source(
        _request(
            "203.0.113.250",
            {"X-Forwarded-For": f"{REAL_CLIENT}, {RENDER_PROXY}"},
        ),
        settings,
    )
    assert resolution.address == "203.0.113.250"
    assert resolution.path == "direct_peer"


@pytest.mark.unit
def test_direct_render_origin_ignores_cf_connecting_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, trusted_cidrs=RENDER_TRUSTED)
    resolution = resolve_admin_login_client_source(
        _request(
            REAL_CONNECTOR,
            {"CF-Connecting-IP": REAL_CLIENT, "X-Forwarded-For": REAL_CLIENT},
        ),
        settings,
    )
    assert resolution.address == REAL_CONNECTOR
    assert resolution.path == "direct_peer"


@pytest.mark.unit
def test_header_precedence_xff_over_forwarded_and_cf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, trusted_cidrs=RENDER_TRUSTED)
    resolution = resolve_admin_login_client_source(
        _request(
            RENDER_PROXY,
            {
                "X-Forwarded-For": REAL_CLIENT,
                "Forwarded": f'for="{ATTACKER_SPOOF}"',
                "CF-Connecting-IP": ATTACKER_SPOOF,
            },
        ),
        settings,
    )
    assert resolution.address == REAL_CLIENT
    assert resolution.path == "trusted_chain"


@pytest.mark.unit
def test_forwarded_header_used_when_xff_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, trusted_cidrs=RENDER_TRUSTED)
    resolution = resolve_admin_login_client_source(
        _request(
            RENDER_PROXY,
            {"Forwarded": f'for="{REAL_CLIENT}";proto=https'},
        ),
        settings,
    )
    assert resolution.address == REAL_CLIENT
    assert resolution.path == "trusted_forwarded"


@pytest.mark.unit
def test_cf_connecting_ip_used_when_other_headers_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, trusted_cidrs=RENDER_TRUSTED)
    resolution = resolve_admin_login_client_source(
        _request(RENDER_PROXY, {"CF-Connecting-IP": REAL_CLIENT}),
        settings,
    )
    assert resolution.address == REAL_CLIENT
    assert resolution.path == "trusted_cf_connecting_ip"


@pytest.mark.unit
def test_address_format_edge_cases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, trusted_cidrs=RENDER_TRUSTED)
    ipv6 = resolve_admin_login_client_source(
        _request(RENDER_PROXY, {"X-Forwarded-For": "2001:db8::5"}),
        settings,
    )
    assert ipv6.address == "2001:db8::5"

    mapped = resolve_admin_login_client_source(
        _request(RENDER_PROXY, {"X-Forwarded-For": "::ffff:203.0.113.77"}),
        settings,
    )
    assert mapped.address == "203.0.113.77"

    whitespace = resolve_admin_login_client_source(
        _request(RENDER_PROXY, {"X-Forwarded-For": f"  {REAL_CLIENT}  ,  "}),
        settings,
    )
    assert whitespace.address == REAL_CLIENT

    empty_elements = resolve_admin_login_client_source(
        _request(RENDER_PROXY, {"X-Forwarded-For": f",,{REAL_CLIENT},"}),
        settings,
    )
    assert empty_elements.address == REAL_CLIENT

    invalid = resolve_admin_login_client_source(
        _request(RENDER_PROXY, {"X-Forwarded-For": "not-valid, also-bad"}),
        settings,
    )
    assert invalid.address == "unknown"
    assert invalid.path == "invalid_forwarding_ignored"

    overlong = ",".join([f"203.0.113.{index}" for index in range(40)])
    too_long = resolve_admin_login_client_source(
        _request(RENDER_PROXY, {"X-Forwarded-For": overlong}),
        settings,
    )
    assert too_long.address == "unknown"
    assert too_long.path == "invalid_forwarding_ignored"


@pytest.mark.unit
@pytest.mark.integration
def test_limiter_rotating_spoofed_headers_single_source_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", "operator")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", admin_auth._password_hasher.hash("pw"))
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "test-session-secret-32chars-minimum")
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "3")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", RENDER_TRUSTED)

    from tests.test_admin_auth import FakeRateLimitStore, shared_rate_limiter

    store = FakeRateLimitStore()
    keys_seen: set[str] = set()

    with shared_rate_limiter(store):
        for index in range(5):
            source = resolve_admin_login_client_source(
                _request(
                    RENDER_PROXY,
                    {"X-Forwarded-For": f"{ATTACKER_SPOOF}.{index}, {REAL_CONNECTOR}"},
                ),
                get_settings(),
            ).address
            key = admin_auth.build_source_rate_limit_key(source)
            keys_seen.add(key)
            admin_auth.try_admit_login_attempt(
                _request(
                    RENDER_PROXY,
                    {"X-Forwarded-For": f"{ATTACKER_SPOOF}.{index}, {REAL_CONNECTOR}"},
                ),
                get_settings(),
            )

    assert len(keys_seen) == 1
    assert len(store.rows) == 1


@pytest.mark.unit
def test_deployment_proxy_settings_present_and_consistent() -> None:
    render_yaml = (Path(__file__).resolve().parents[1] / "render.yaml").read_text(
        encoding="utf-8"
    )
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in render_yaml
    assert "ADMIN_FORWARDED_ALLOW_IPS" in render_yaml
    assert "--proxy-headers" in render_yaml
    assert '--forwarded-allow-ips "$ADMIN_FORWARDED_ALLOW_IPS"' in render_yaml

    trusted_match = re.search(
        r'ADMIN_TRUSTED_PROXY_CIDRS\n\s+value:\s+"([^"]+)"',
        render_yaml,
    )
    forwarded_match = re.search(
        r'ADMIN_FORWARDED_ALLOW_IPS\n\s+value:\s+"([^"]+)"',
        render_yaml,
    )
    assert trusted_match is not None
    assert forwarded_match is not None
    assert "127.0.0.1" in forwarded_match.group(1)
    assert "10.0.0.0/8" in trusted_match.group(1)


@pytest.mark.unit
def test_privacy_no_raw_forwarding_in_logs_or_limiter_rows(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = _settings(monkeypatch, trusted_cidrs=RENDER_TRUSTED)
    caplog.set_level("INFO")
    chain = f"{ATTACKER_SPOOF}, {REAL_CONNECTOR}"
    with caplog.at_level("INFO"):
        reset_client_source_telemetry_for_tests()
        resolution = resolve_admin_login_client_source(
            _request(RENDER_PROXY, {"X-Forwarded-For": chain}),
            settings,
        )
    key = admin_auth.build_source_rate_limit_key(resolution.address)
    assert ATTACKER_SPOOF not in key
    assert REAL_CONNECTOR not in key
    assert chain not in caplog.text
    assert ATTACKER_SPOOF not in caplog.text
    assert "trusted_chain" in resolution.path


@pytest.mark.unit
def test_legacy_admin_trust_proxy_headers_enables_render_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, legacy_trust=True)
    assert settings.admin_trusted_proxy_cidrs == DEFAULT_RENDER_TRUSTED_PROXY_CIDRS


@pytest.mark.unit
def test_trusted_proxy_missing_forwarding_uses_unknown_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, trusted_cidrs=RENDER_TRUSTED)
    resolution = resolve_admin_login_client_source(_request(RENDER_PROXY), settings)
    assert resolution.address == "unknown"
    assert resolution.path == "missing_forwarding"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.mark.integration
def test_uvicorn_proxy_configuration_resolves_trusted_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", "10.0.0.0/8")
    monkeypatch.setenv("ADMIN_FORWARDED_ALLOW_IPS", DEFAULT_RENDER_FORWARDED_ALLOW_IPS)
    monkeypatch.setenv("ADMIN_LOGIN_TEST_PEER_HEADER", "1")
    monkeypatch.setenv("BASE_URL", "http://testserver")

    from fastapi import FastAPI

    probe_app = FastAPI()

    @probe_app.get("/probe")
    def probe(request: Request) -> dict[str, str]:
        settings = get_settings()
        resolution = resolve_admin_login_client_source(request, settings)
        return {"path": resolution.path, "address": resolution.address}

    port = _free_port()
    config = uvicorn.Config(
        probe_app,
        host="127.0.0.1",
        port=port,
        log_level="error",
        forwarded_allow_ips=DEFAULT_RENDER_FORWARDED_ALLOW_IPS,
        proxy_headers=True,
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
        pytest.fail("uvicorn server did not start")

    try:
        direct = httpx.get(
            f"http://127.0.0.1:{port}/probe",
            timeout=2.0,
        )
        assert direct.status_code == 200
        assert direct.json()["path"] == "direct_peer"
        assert direct.json()["address"] == "127.0.0.1"

        trusted = httpx.get(
            f"http://127.0.0.1:{port}/probe",
            headers={
                "X-Forwarded-For": f"{ATTACKER_SPOOF}, {REAL_CONNECTOR}",
                "X-Test-Immediate-Peer": RENDER_PROXY,
            },
            timeout=2.0,
        )
        assert trusted.status_code == 200
        assert trusted.json()["path"] == "trusted_chain"
        assert trusted.json()["address"] == REAL_CONNECTOR
    finally:
        server.should_exit = True
        thread.join(timeout=5)
