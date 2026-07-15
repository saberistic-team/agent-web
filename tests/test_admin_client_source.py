"""Tests for trusted-proxy admin login client source resolution (#239)."""

from __future__ import annotations

import hashlib
import logging
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import Request

from app import admin_auth
from app.admin_client_source import (
    ClientSourcePath,
    client_source_for_limiter,
    normalize_ip,
    parse_forwarded_for_chain,
    parse_forwarded_header,
    parse_trusted_networks,
    resolve_admin_login_client_source,
)
from app.config import get_settings
from tests.test_admin_auth import (
    FakeRateLimitStore,
    TEST_USERNAME,
    shared_rate_limiter,
)

RENDER_LB = "10.0.0.1"
RENDER_CIDRS = "10.0.0.0/8"
CF_EDGE = "198.51.100.1"
CF_CIDRS = "198.51.100.0/24"
CLIENT = "203.0.113.50"
SPOOFED = "203.0.113.99"
ATTACKER = "192.0.2.50"
UNKNOWN = "unknown"


def _make_request(
    peer: str,
    headers: dict[str, str] | None = None,
) -> Request:
    scope: dict[str, Any] = {
        "type": "http",
        "headers": [
            (name.lower().encode("ascii"), value.encode("ascii"))
            for name, value in (headers or {}).items()
        ],
        "client": (peer, 12345),
        "method": "POST",
        "path": "/admin/login",
    }
    return Request(scope)


def _settings_with_proxy_trust(
    monkeypatch: pytest.MonkeyPatch,
    *,
    render_cidrs: str = RENDER_CIDRS,
    cloudflare_cidrs: str = CF_CIDRS,
) -> Any:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", render_cidrs)
    monkeypatch.setenv("ADMIN_CLOUDFLARE_PROXY_CIDRS", cloudflare_cidrs)
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    return get_settings()


@pytest.fixture
def rate_limit_store() -> FakeRateLimitStore:
    return FakeRateLimitStore()


@pytest.fixture
def proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", "unused")
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "test-session-secret-32chars-minimum")
    monkeypatch.setenv("BASE_URL", "http://testserver")
    _settings_with_proxy_trust(monkeypatch)
    admin_auth.reset_login_rate_limiter()


@pytest.mark.unit
def test_normalize_ip_formats() -> None:
    assert normalize_ip("203.0.113.1") == "203.0.113.1"
    assert normalize_ip("203.0.113.1:8080") == "203.0.113.1"
    assert normalize_ip("2001:0db8::1") == "2001:db8::1"
    assert normalize_ip("[2001:db8::1]:443") == "2001:db8::1"
    assert normalize_ip("::ffff:203.0.113.1") == "203.0.113.1"
    assert normalize_ip("  203.0.113.1  ") == "203.0.113.1"
    assert normalize_ip("") is None
    assert normalize_ip("not-an-ip") is None
    assert normalize_ip("999.999.999.999") is None


@pytest.mark.unit
def test_parse_forwarded_for_chain_strips_empty_elements() -> None:
    assert parse_forwarded_for_chain("203.0.113.1, , 10.0.0.1") == [
        "203.0.113.1",
        "10.0.0.1",
    ]


@pytest.mark.unit
def test_parse_forwarded_header_ipv6_and_quoted() -> None:
    header = 'for="[2001:db8::1]:443";proto=https, for=203.0.113.1'
    assert parse_forwarded_header(header) == ["2001:db8::1", "203.0.113.1"]


@pytest.mark.unit
def test_direct_spoof_single_and_multi_value_xff_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_proxy_trust(monkeypatch, render_cidrs="")
    single = resolve_admin_login_client_source(
        _make_request(ATTACKER, {"X-Forwarded-For": SPOOFED}),
        settings,
    )
    multi = resolve_admin_login_client_source(
        _make_request(
            ATTACKER,
            {"X-Forwarded-For": f"{SPOOFED}, {CLIENT}, {RENDER_LB}"},
        ),
        settings,
    )
    assert single.source == ATTACKER
    assert single.path is ClientSourcePath.DIRECT_PEER
    assert multi.source == ATTACKER
    assert multi.path is ClientSourcePath.DIRECT_PEER


@pytest.mark.unit
def test_cloudflare_append_ignores_attacker_leftmost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_proxy_trust(monkeypatch)
    resolution = resolve_admin_login_client_source(
        _make_request(
            RENDER_LB,
            {
                "X-Forwarded-For": f"{SPOOFED}, {CLIENT}, {CF_EDGE}",
                "CF-Connecting-IP": CLIENT,
            },
        ),
        settings,
    )
    assert resolution.source == CLIENT
    assert resolution.path is ClientSourcePath.CF_CONNECTING_IP


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings_with_proxy_trust(monkeypatch)
    resolution = resolve_admin_login_client_source(
        _make_request(
            RENDER_LB,
            {"X-Forwarded-For": f"{CLIENT}, {RENDER_LB}"},
        ),
        settings,
    )
    assert resolution.source == CLIENT
    assert resolution.path is ClientSourcePath.TRUSTED_XFF_CHAIN


@pytest.mark.unit
def test_partial_trust_untrusted_intermediary_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_proxy_trust(monkeypatch)
    resolution = resolve_admin_login_client_source(
        _make_request(
            RENDER_LB,
            {"X-Forwarded-For": f"{CLIENT}, {ATTACKER}, {RENDER_LB}"},
        ),
        settings,
    )
    assert resolution.source == UNKNOWN
    assert resolution.path is ClientSourcePath.MALFORMED


@pytest.mark.unit
def test_direct_render_origin_ignores_spoofed_cloudflare_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_proxy_trust(monkeypatch)
    resolution = resolve_admin_login_client_source(
        _make_request(
            ATTACKER,
            {
                "CF-Connecting-IP": SPOOFED,
                "X-Forwarded-For": SPOOFED,
                "Forwarded": f'for={SPOOFED};proto=https',
            },
        ),
        settings,
    )
    assert resolution.source == ATTACKER
    assert resolution.path is ClientSourcePath.DIRECT_PEER


@pytest.mark.unit
def test_header_precedence_cf_over_conflicting_xff_and_forwarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_proxy_trust(monkeypatch)
    resolution = resolve_admin_login_client_source(
        _make_request(
            RENDER_LB,
            {
                "CF-Connecting-IP": CLIENT,
                "X-Forwarded-For": f"{SPOOFED}, {CF_EDGE}",
                "Forwarded": f'for={SPOOFED};proto=https',
            },
        ),
        settings,
    )
    assert resolution.source == CLIENT
    assert resolution.path is ClientSourcePath.CF_CONNECTING_IP


@pytest.mark.unit
def test_overlong_forward_chain_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings_with_proxy_trust(monkeypatch)
    long_chain = ", ".join([f"203.0.113.{index}" for index in range(40)])
    resolution = resolve_admin_login_client_source(
        _make_request(RENDER_LB, {"X-Forwarded-For": long_chain}),
        settings,
    )
    assert resolution.source == UNKNOWN
    assert resolution.path is ClientSourcePath.MALFORMED


@pytest.mark.unit
def test_cf_connecting_ip_without_cloudflare_hop_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = _settings_with_proxy_trust(monkeypatch)
    with caplog.at_level(logging.WARNING):
        resolution = resolve_admin_login_client_source(
            _make_request(
                RENDER_LB,
                {
                    "CF-Connecting-IP": SPOOFED,
                    "X-Forwarded-For": f"{ATTACKER}, {RENDER_LB}",
                },
            ),
            settings,
        )
    assert resolution.source == ATTACKER
    assert resolution.path is ClientSourcePath.TRUSTED_XFF_CHAIN
    assert any(
        getattr(record, "reason", None) == "cf_connecting_ip_without_cloudflare_hop"
        for record in caplog.records
    )


@pytest.mark.unit
def test_missing_peer_maps_to_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings_with_proxy_trust(monkeypatch, render_cidrs="")
    scope: dict[str, Any] = {
        "type": "http",
        "headers": [],
        "method": "POST",
        "path": "/admin/login",
    }
    resolution = resolve_admin_login_client_source(Request(scope), settings)
    assert resolution.source == UNKNOWN
    assert resolution.path is ClientSourcePath.UNKNOWN


@pytest.mark.unit
def test_rotating_spoofed_headers_share_one_limiter_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_proxy_trust(monkeypatch)
    keys: set[str] = set()
    for index in range(5):
        request = _make_request(
            RENDER_LB,
            {"X-Forwarded-For": f"203.0.113.{index}, {CF_EDGE}"},
        )
        source = client_source_for_limiter(request, settings)
        keys.add(admin_auth.build_source_rate_limit_key(source))
    assert len(keys) == 1
    assert all(
        client_source_for_limiter(
            _make_request(RENDER_LB, {"X-Forwarded-For": f"203.0.113.{index}, {CF_EDGE}"}),
            settings,
        )
        == UNKNOWN
        for index in range(5)
    )


@pytest.mark.unit
@pytest.mark.integration
def test_limiter_admissions_do_not_multiply_on_spoofed_headers(
    monkeypatch: pytest.MonkeyPatch,
    rate_limit_store: FakeRateLimitStore,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "3")
    _settings_with_proxy_trust(monkeypatch)

    def admit_with_xff(spoofed: str) -> bool:
        request = _make_request(
            RENDER_LB,
            {"X-Forwarded-For": f"{spoofed}, {CF_EDGE}"},
        )
        result = admin_auth.try_admit_login_attempt(request, get_settings(), username="ghost")
        return result.admitted

    with shared_rate_limiter(rate_limit_store):
        assert admit_with_xff("203.0.113.1") is True
        assert admit_with_xff("203.0.113.2") is True
        assert admit_with_xff("203.0.113.3") is True
        assert admit_with_xff("203.0.113.4") is False
    assert len(rate_limit_store.rows) == 1


@pytest.mark.unit
def test_privacy_resolution_logs_exclude_raw_addresses(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = _settings_with_proxy_trust(monkeypatch)
    request = _make_request(
        RENDER_LB,
        {"X-Forwarded-For": f"{CLIENT}, {CF_EDGE}", "CF-Connecting-IP": CLIENT},
    )
    with caplog.at_level(logging.DEBUG):
        source = client_source_for_limiter(request, settings)
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:12]
    joined = "\n".join(f"{record.getMessage()} {record.__dict__}" for record in caplog.records)
    assert CLIENT not in joined
    assert CF_EDGE not in joined
    assert digest in joined or source == CLIENT


@pytest.mark.unit
def test_render_yaml_proxy_settings_are_consistent() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    render_text = (repo_root / "render.yaml").read_text(encoding="utf-8")

    allow_ips_match = re.search(r"--forwarded-allow-ips=([^\s\n]+)", render_text)
    assert allow_ips_match is not None
    allow_ips = set(allow_ips_match.group(1).split(","))

    render_match = re.search(
        r"ADMIN_TRUSTED_PROXY_CIDRS\n\s+value:\s+\"([^\"]+)\"",
        render_text,
    )
    cloudflare_match = re.search(
        r"ADMIN_CLOUDFLARE_PROXY_CIDRS\n\s+value:\s+\"([^\"]+)\"",
        render_text,
    )
    assert render_match is not None
    assert cloudflare_match is not None

    render_cidrs = set(render_match.group(1).split(","))
    assert allow_ips == render_cidrs
    assert parse_trusted_networks(cloudflare_match.group(1).split(","))


@pytest.mark.integration
def test_uvicorn_forwarded_allow_ips_matches_render_start_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the same uvicorn proxy boundary declared in render.yaml."""
    repo_root = Path(__file__).resolve().parents[1]
    render_text = (repo_root / "render.yaml").read_text(encoding="utf-8")
    allow_ips_match = re.search(r"--forwarded-allow-ips=([^\s\n]+)", render_text)
    assert allow_ips_match is not None
    allow_ips = allow_ips_match.group(1)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    env = {
        **dict(__import__("os").environ),
        "DATABASE_URL": "",
        "ADMIN_USERNAME": "",
        "ADMIN_PASSWORD_HASH": "",
        "ADMIN_SESSION_SECRET": "",
    }
    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        f"--forwarded-allow-ips={allow_ips}",
    ]
    proc = subprocess.Popen(
        cmd,
        cwd=repo_root,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                response = httpx.get(f"http://127.0.0.1:{port}/health", timeout=1.0)
                if response.status_code == 200:
                    break
            except (httpx.HTTPError, OSError):
                time.sleep(0.2)
        else:
            pytest.fail("uvicorn did not become ready")

        monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", "127.0.0.1/32")
        monkeypatch.setenv("ADMIN_CLOUDFLARE_PROXY_CIDRS", CF_CIDRS)
        settings = get_settings()

        resolution = resolve_admin_login_client_source(
            _make_request(
                "127.0.0.1",
                {
                    "X-Forwarded-For": f"{SPOOFED}, {CLIENT}",
                },
            ),
            settings,
        )
        assert resolution.source == UNKNOWN
        assert resolution.path is ClientSourcePath.MALFORMED

        trusted = resolve_admin_login_client_source(
            _make_request("127.0.0.1", {"X-Forwarded-For": CLIENT}),
            settings,
        )
        assert trusted.source == CLIENT
        assert trusted.path is ClientSourcePath.TRUSTED_XFF_CHAIN
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@pytest.mark.unit
@pytest.mark.integration
def test_login_rate_limit_uses_trusted_chain_source(
    monkeypatch: pytest.MonkeyPatch,
    rate_limit_store: FakeRateLimitStore,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    settings = _settings_with_proxy_trust(monkeypatch)

    def admit(client_ip: str) -> bool:
        request = _make_request(
            RENDER_LB,
            {
                "X-Forwarded-For": f"{client_ip}, {CF_EDGE}",
                "CF-Connecting-IP": client_ip,
            },
        )
        return admin_auth.try_admit_login_attempt(
            request, settings, username="ghost"
        ).admitted

    with shared_rate_limiter(rate_limit_store):
        assert admit("203.0.113.77") is True
        assert admit("203.0.113.77") is True
        assert admit("203.0.113.77") is False
        assert admit("203.0.113.88") is True
