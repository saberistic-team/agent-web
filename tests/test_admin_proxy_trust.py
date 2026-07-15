"""Tests for trusted-proxy admin login client source resolution (#239)."""

from __future__ import annotations

import json
import os
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
from app.config import get_settings
from app.proxy_trust import (
    default_trusted_proxy_ips_spec,
    normalize_client_address,
    reset_invalid_forwarding_telemetry,
    resolve_admin_login_client_source,
)
from tests.test_admin_auth import FakeRateLimitStore

REPO_ROOT = Path(__file__).resolve().parent.parent
RENDER_LB = "10.0.0.1"
CLOUDFLARE_EDGE = "103.21.244.1"
REAL_CLIENT = "203.0.113.50"
SPOOFED_CLIENT = "198.51.100.99"
UNTRUSTED_PEER = "198.51.100.10"


def _request(
    *,
    peer: str,
    headers: dict[str, str] | None = None,
) -> Request:
    encoded_headers = [
        (key.lower().encode("latin-1"), value.encode("latin-1"))
        for key, value in (headers or {}).items()
    ]
    scope = {
        "type": "http",
        "headers": encoded_headers,
        "client": (peer, 12345),
        "method": "POST",
        "path": "/admin/login",
    }
    return Request(scope)


def _settings(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> Any:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", overrides.get("trust", "true"))
    if "trusted_ips" in overrides:
        monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", overrides["trusted_ips"])
    else:
        monkeypatch.setenv(
            "ADMIN_TRUSTED_PROXY_IPS",
            f"{RENDER_LB}/32,{CLOUDFLARE_EDGE}/32,127.0.0.1",
        )
    if "cloudflare_ips" in overrides:
        monkeypatch.setenv("ADMIN_CLOUDFLARE_EDGE_IPS", overrides["cloudflare_ips"])
    else:
        monkeypatch.delenv("ADMIN_CLOUDFLARE_EDGE_IPS", raising=False)
    return get_settings()


@pytest.fixture
def rate_limit_store() -> FakeRateLimitStore:
    return FakeRateLimitStore()


@pytest.fixture(autouse=True)
def _reset_telemetry() -> None:
    reset_invalid_forwarding_telemetry()


@pytest.mark.unit
def test_direct_spoof_single_and_multi_value_xff_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, trust="false")
    for header in (
        SPOOFED_CLIENT,
        f"{SPOOFED_CLIENT}, {REAL_CLIENT}",
    ):
        request = _request(peer=UNTRUSTED_PEER, headers={"X-Forwarded-For": header})
        resolution = resolve_admin_login_client_source(request, settings)
        assert resolution.source == UNTRUSTED_PEER
        assert resolution.path == "direct_peer"
        assert resolution.invalid_forwarding is False


@pytest.mark.unit
def test_cloudflare_append_selects_real_client_not_leftmost_spoof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    xff = f"{SPOOFED_CLIENT}, {REAL_CLIENT}, {CLOUDFLARE_EDGE}"
    request = _request(peer=RENDER_LB, headers={"X-Forwarded-For": xff})
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
    assert resolution.path == "xff_trusted_chain"


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch)
    xff = f"{REAL_CLIENT}, {CLOUDFLARE_EDGE}"
    request = _request(peer=RENDER_LB, headers={"X-Forwarded-For": xff})
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
    assert resolution.path == "xff_trusted_chain"


@pytest.mark.unit
def test_partial_trust_untrusted_intermediary_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    xff = f"{REAL_CLIENT}, {UNTRUSTED_PEER}"
    request = _request(peer=RENDER_LB, headers={"X-Forwarded-For": xff})
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == UNTRUSTED_PEER
    assert resolution.path == "xff_trusted_chain"


@pytest.mark.unit
def test_direct_render_origin_ignores_spoofed_cf_connecting_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    request = _request(
        peer=UNTRUSTED_PEER,
        headers={
            "CF-Connecting-IP": REAL_CLIENT,
            "X-Forwarded-For": REAL_CLIENT,
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == UNTRUSTED_PEER
    assert resolution.path == "untrusted_forwarding_peer"


@pytest.mark.unit
def test_cf_connecting_ip_used_when_edge_hop_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    request = _request(
        peer=RENDER_LB,
        headers={
            "CF-Connecting-IP": REAL_CLIENT,
            "X-Forwarded-For": CLOUDFLARE_EDGE,
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
    assert resolution.path == "cf_connecting_ip"


@pytest.mark.unit
def test_header_precedence_xff_over_cf_and_forwarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    xff = f"{REAL_CLIENT}, {CLOUDFLARE_EDGE}"
    request = _request(
        peer=RENDER_LB,
        headers={
            "X-Forwarded-For": xff,
            "CF-Connecting-IP": SPOOFED_CLIENT,
            "Forwarded": f'for={SPOOFED_CLIENT};proto=https, for="{CLOUDFLARE_EDGE}"',
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
    assert resolution.path == "xff_trusted_chain"


@pytest.mark.unit
def test_forwarded_header_used_when_xff_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch)
    request = _request(
        peer=RENDER_LB,
        headers={
            "Forwarded": f'for={REAL_CLIENT};proto=https, for="{CLOUDFLARE_EDGE}"',
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
    assert resolution.path == "forwarded_header"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("203.0.113.1", "203.0.113.1"),
        ("203.0.113.1:443", "203.0.113.1"),
        ("2001:db8::1", "2001:db8::1"),
        ("[2001:db8::1]:443", "2001:db8::1"),
        ("::ffff:203.0.113.1", "203.0.113.1"),
        ("  203.0.113.1  ", "203.0.113.1"),
        ("", None),
        ("not-an-ip", None),
    ],
)
@pytest.mark.unit
def test_normalize_client_address_formats(raw: str, expected: str | None) -> None:
    assert normalize_client_address(raw) == expected


@pytest.mark.unit
def test_malformed_forwarding_chain_returns_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    overlong = ", ".join([f"10.0.0.{index}" for index in range(25)])
    request = _request(peer=RENDER_LB, headers={"X-Forwarded-For": overlong})
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "unknown"
    assert resolution.path == "malformed_x_forwarded_for"


@pytest.mark.unit
def test_empty_xff_elements_are_malformed(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch)
    request = _request(peer=RENDER_LB, headers={"X-Forwarded-For": " , , "})
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "unknown"
    assert resolution.path == "malformed_x_forwarded_for"


@pytest.mark.unit
def test_all_trusted_xff_without_vendor_header_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    request = _request(
        peer=RENDER_LB,
        headers={"X-Forwarded-For": f"{CLOUDFLARE_EDGE}, {RENDER_LB}"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "unknown"
    assert resolution.path == "malformed_forwarding"


@pytest.mark.unit
def test_all_trusted_xff_hops_use_cf_connecting_ip_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    request = _request(
        peer=RENDER_LB,
        headers={
            "X-Forwarded-For": f"{CLOUDFLARE_EDGE}",
            "CF-Connecting-IP": REAL_CLIENT,
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
    assert resolution.path == "cf_connecting_ip"


@pytest.mark.unit
def test_rotating_spoofed_headers_share_one_limiter_bucket(
    monkeypatch: pytest.MonkeyPatch,
    rate_limit_store: Any,
) -> None:
    from tests.test_admin_auth import shared_rate_limiter

    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "false")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    with shared_rate_limiter(rate_limit_store):
        keys: set[str] = set()
        for index in range(4):
            request = _request(
                peer=UNTRUSTED_PEER,
                headers={"X-Forwarded-For": f"203.0.113.{index}"},
            )
            settings = get_settings()
            source = admin_auth.client_ip(request, settings)
            keys.add(admin_auth.build_source_rate_limit_key(source))
            admission = admin_auth.try_admit_login_attempt(
                request,
                settings,
                username="ghost",
            )
            if index < 2:
                assert admission.admitted
            else:
                assert admission.throttled
        assert len(keys) == 1


@pytest.mark.unit
def test_telemetry_logs_resolution_path_without_raw_addresses(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from contextlib import contextmanager
    from unittest.mock import MagicMock

    from app import db

    @contextmanager
    def fake_conn(_database_url: str):
        yield MagicMock()

    monkeypatch.setattr(
        "app.admin_auth.db.try_admit_admin_login",
        lambda *args, **kwargs: db.AdminLoginAdmission(
            admitted=True,
            throttled=False,
            already_locked=False,
            lockout_transition=False,
        ),
    )
    monkeypatch.setattr(
        "app.admin_auth.db.cleanup_expired_admin_login_rate_limits",
        lambda *args, **kwargs: 0,
    )
    monkeypatch.setattr("app.admin_auth.db.db_connection", fake_conn)

    settings = _settings(monkeypatch)
    request = _request(
        peer=RENDER_LB,
        headers={"X-Forwarded-For": f"{REAL_CLIENT}, {CLOUDFLARE_EDGE}"},
    )
    with caplog.at_level("INFO"):
        admin_auth.try_admit_login_attempt(request, settings, username="ghost")
    assert REAL_CLIENT not in caplog.text
    assert CLOUDFLARE_EDGE not in caplog.text
    assert any(
        record.__dict__.get("source_resolution_path") == "xff_trusted_chain"
        for record in caplog.records
    )


@pytest.mark.unit
def test_limiter_rows_store_only_digests(
    monkeypatch: pytest.MonkeyPatch,
    rate_limit_store: Any,
) -> None:
    from tests.test_admin_auth import shared_rate_limiter

    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", f"{RENDER_LB}/32,{CLOUDFLARE_EDGE}/32")
    with shared_rate_limiter(rate_limit_store):
        request = _request(
            peer=RENDER_LB,
            headers={"X-Forwarded-For": f"{REAL_CLIENT}, {CLOUDFLARE_EDGE}"},
        )
        admin_auth.try_admit_login_attempt(request, get_settings(), username="ghost")
    for row in rate_limit_store.rows.values():
        serialized = json.dumps(row, default=str)
        assert REAL_CLIENT not in serialized
        assert "X-Forwarded-For" not in serialized


@pytest.mark.unit
def test_render_yaml_proxy_settings_are_consistent() -> None:
    render_yaml = (REPO_ROOT / "render.yaml").read_text(encoding="utf-8")
    start_script = (REPO_ROOT / "scripts" / "start_web.sh").read_text(encoding="utf-8")
    assert "bash scripts/start_web.sh" in render_yaml
    assert "ADMIN_TRUST_PROXY_HEADERS" in render_yaml
    assert "ADMIN_TRUSTED_PROXY_IPS" in render_yaml
    assert "--forwarded-allow-ips" in start_script
    assert "ADMIN_TRUSTED_PROXY_IPS" in start_script


@pytest.mark.unit
def test_health_reports_proxy_trust_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.main import health

    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", "10.0.0.0/8,127.0.0.1")
    summary = health()["admin_login_proxy_trust"]
    assert summary["enabled"] is True
    assert summary["trusted_network_count"] == 2


@pytest.mark.integration
def test_uvicorn_proxy_configuration_matches_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise Uvicorn ProxyHeadersMiddleware with production forwarded-allow-ips."""
    port = _free_port()
    trusted_ips = "127.0.0.1"
    env = os.environ.copy()
    env["PORT"] = str(port)
    env["ADMIN_TRUST_PROXY_HEADERS"] = "true"
    env["ADMIN_TRUSTED_PROXY_IPS"] = trusted_ips
    env["DATABASE_URL"] = ""
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--forwarded-allow-ips",
            trusted_ips,
        ],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for_port(port)
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5.0) as http:
            health = http.get("/health").json()
            assert health["admin_login_proxy_trust"]["enabled"] is True

            # Confirm the stack serves traffic behind Uvicorn proxy middleware with
            # spoofed forwarding headers present (must not crash).
            response = http.get(
                "/hello",
                headers={"X-Forwarded-For": "198.51.100.77, 203.0.113.88"},
            )
            assert response.status_code == 200
            assert response.json() == {"message": "hello world"}
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@pytest.mark.unit
def test_default_trusted_proxy_ips_spec_matches_render() -> None:
    render_yaml = (REPO_ROOT / "render.yaml").read_text(encoding="utf-8")
    for cidr in default_trusted_proxy_ips_spec().split(","):
        assert cidr in render_yaml


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_port(port: int, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"uvicorn did not start on port {port}")
