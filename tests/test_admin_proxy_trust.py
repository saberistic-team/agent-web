"""Trusted-proxy admin login source resolution (#239)."""

from __future__ import annotations

import logging
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from argon2 import PasswordHasher
from fastapi import Request
from fastapi.testclient import TestClient

from app import admin_auth
from app.config import get_settings
from app.main import app
from app.proxy_trust import (
    RENDER_TRUSTED_PROXY_CIDRS,
    SourceResolutionPath,
    normalize_ip_address,
    parse_trusted_proxy_cidrs,
    resolve_admin_login_client_source,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
RENDER_YAML = REPO_ROOT / "render.yaml"

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "test-session-secret-32chars-minimum"
TRUSTED_PROXY_CIDRS = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.1/32"
TRUSTED_PEER = "10.0.0.5"
UNTRUSTED_PEER = "203.0.113.77"


@pytest.fixture(autouse=True)
def _admin_proxy_trust_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "5")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_WINDOW_SECONDS", "900")
    monkeypatch.setenv("ADMIN_LOGIN_LOCKOUT_SECONDS", "900")
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    admin_auth.reset_login_rate_limiter()


def _request(
    *,
    peer: str,
    headers: dict[str, str] | None = None,
) -> Request:
    encoded_headers = [
        (key.lower().encode("ascii"), value.encode("ascii"))
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


def _settings(monkeypatch: pytest.MonkeyPatch, *, trust_proxy: bool) -> Any:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    if trust_proxy:
        monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
        monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", TRUSTED_PROXY_CIDRS)
    else:
        monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
        monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    return get_settings()


@pytest.mark.unit
def test_direct_spoof_single_value_xff_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch, trust_proxy=False)
    request = _request(peer=UNTRUSTED_PEER, headers={"X-Forwarded-For": "203.0.113.99"})
    result = resolve_admin_login_client_source(request, settings)
    assert result.source == UNTRUSTED_PEER
    assert result.path is SourceResolutionPath.DIRECT_PEER


@pytest.mark.unit
def test_direct_spoof_multi_value_xff_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch, trust_proxy=False)
    request = _request(
        peer=UNTRUSTED_PEER,
        headers={"X-Forwarded-For": "203.0.113.1, 203.0.113.2, 203.0.113.3"},
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.source == UNTRUSTED_PEER
    assert result.path is SourceResolutionPath.DIRECT_PEER


@pytest.mark.unit
def test_cloudflare_append_selects_connecting_client_not_leftmost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, trust_proxy=True)
    request = _request(
        peer=TRUSTED_PEER,
        headers={"X-Forwarded-For": "203.0.113.99, 198.51.100.10"},
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.source == "198.51.100.10"
    assert result.path is SourceResolutionPath.TRUSTED_XFF


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch, trust_proxy=True)
    request = _request(
        peer=TRUSTED_PEER,
        headers={"X-Forwarded-For": "198.51.100.20, 10.0.0.1"},
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.source == "198.51.100.20"
    assert result.path is SourceResolutionPath.TRUSTED_XFF


@pytest.mark.unit
def test_partial_trust_untrusted_intermediary_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, trust_proxy=True)
    request = _request(
        peer="198.51.100.50",
        headers={"X-Forwarded-For": "203.0.113.10, 10.0.0.1"},
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.source == "198.51.100.50"
    assert result.path is SourceResolutionPath.DIRECT_PEER
    assert result.rejected_forwarding is True


@pytest.mark.unit
def test_direct_render_origin_ignores_cf_connecting_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, trust_proxy=True)
    request = _request(
        peer=UNTRUSTED_PEER,
        headers={"CF-Connecting-IP": "203.0.113.55"},
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.source == UNTRUSTED_PEER
    assert result.path is SourceResolutionPath.DIRECT_PEER
    assert result.rejected_forwarding is True


@pytest.mark.unit
def test_header_precedence_cf_connecting_ip_over_conflicting_xff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, trust_proxy=True)
    request = _request(
        peer=TRUSTED_PEER,
        headers={
            "CF-Connecting-IP": "198.51.100.30",
            "X-Forwarded-For": "203.0.113.40, 10.0.0.1",
            "Forwarded": 'for=203.0.113.50;proto=https',
        },
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.source == "198.51.100.30"
    assert result.path is SourceResolutionPath.TRUSTED_CF_CONNECTING_IP


@pytest.mark.unit
def test_forwarded_header_used_when_xff_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch, trust_proxy=True)
    request = _request(
        peer=TRUSTED_PEER,
        headers={"Forwarded": 'for=198.51.100.40;proto=https, for=10.0.0.1'},
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.source == "198.51.100.40"
    assert result.path is SourceResolutionPath.TRUSTED_FORWARDED


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("203.0.113.1", "203.0.113.1"),
        ("2001:db8::1", "2001:db8::1"),
        ("203.0.113.1:8080", "203.0.113.1"),
        ("[2001:db8::1]:443", "2001:db8::1"),
        ("::ffff:203.0.113.9", "203.0.113.9"),
        ("  203.0.113.2  ", "203.0.113.2"),
        ("", None),
        ("not-an-ip", None),
        ("", None),
    ],
)
def test_normalize_ip_address_formats(raw: str, expected: str | None) -> None:
    assert normalize_ip_address(raw) == expected


@pytest.mark.unit
def test_xff_whitespace_and_empty_elements(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch, trust_proxy=True)
    request = _request(
        peer=TRUSTED_PEER,
        headers={"X-Forwarded-For": " 198.51.100.60 , , 10.0.0.1 "},
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.source == "198.51.100.60"


@pytest.mark.unit
def test_overlong_forwarding_chain_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch, trust_proxy=True)
    chain = ", ".join(f"203.0.113.{index}" for index in range(12))
    request = _request(peer=TRUSTED_PEER, headers={"X-Forwarded-For": chain})
    result = resolve_admin_login_client_source(request, settings)
    assert result.path is SourceResolutionPath.INVALID_FORWARDING
    assert result.source == TRUSTED_PEER


@pytest.mark.unit
def test_rotating_spoofed_headers_share_one_limiter_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, trust_proxy=False)
    keys = {
        admin_auth.build_source_rate_limit_key(
            resolve_admin_login_client_source(
                _request(
                    peer=UNTRUSTED_PEER,
                    headers={"X-Forwarded-For": f"203.0.113.{index}"},
                ),
                settings,
            ).source
        )
        for index in range(5)
    }
    assert len(keys) == 1


@pytest.mark.unit
def test_trusted_proxy_rotating_leftmost_does_not_create_new_source_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, trust_proxy=True)
    keys = {
        admin_auth.build_source_rate_limit_key(
            resolve_admin_login_client_source(
                _request(
                    peer=TRUSTED_PEER,
                    headers={
                        "X-Forwarded-For": f"203.0.113.{index}, 198.51.100.70",
                    },
                ),
                settings,
            ).source
        )
        for index in range(5)
    }
    assert len(keys) == 1


@pytest.mark.unit
def test_render_yaml_proxy_trust_settings_are_consistent() -> None:
    text = RENDER_YAML.read_text(encoding="utf-8")
    assert "--forwarded-allow-ips" in text
    assert RENDER_TRUSTED_PROXY_CIDRS in text
    assert 'ADMIN_TRUST_PROXY_HEADERS' in text
    assert 'value: "true"' in text
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in text
    assert "UVICORN_FORWARDED_ALLOW_IPS" in text
    assert "--proxy-headers" not in text


@pytest.mark.unit
def test_render_trusted_proxy_cidrs_match_uvicorn_allow_ips() -> None:
    assert parse_trusted_proxy_cidrs(RENDER_TRUSTED_PROXY_CIDRS)
    assert RENDER_TRUSTED_PROXY_CIDRS == TRUSTED_PROXY_CIDRS


@pytest.mark.unit
def test_health_reports_proxy_trust_without_raw_addresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", TRUSTED_PROXY_CIDRS)
    monkeypatch.setenv("UVICORN_FORWARDED_ALLOW_IPS", TRUSTED_PROXY_CIDRS)
    client = TestClient(app)
    payload = client.get("/health").json()
    trust = payload["admin_login_proxy_trust"]
    assert trust["enabled"] is True
    assert trust["trusted_proxy_cidr_count"] == 4
    assert trust["uvicorn_forwarded_allow_ips_configured"] is True
    serialized = str(payload)
    assert "10.0.0." not in serialized
    assert "x-forwarded-for" not in serialized.lower()


@pytest.mark.unit
def test_limiter_and_logs_contain_no_raw_forwarding_data(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = _settings(monkeypatch, trust_proxy=True)
    request = _request(
        peer=UNTRUSTED_PEER,
        headers={
            "X-Forwarded-For": "203.0.113.88, 203.0.113.89",
            "CF-Connecting-IP": "203.0.113.90",
        },
    )
    with caplog.at_level(logging.INFO):
        source_result = resolve_admin_login_client_source(request, settings)
        with patch("app.admin_auth.db.try_admit_admin_login", side_effect=Exception("skip db")):
            with patch(
                "app.admin_auth.db.cleanup_expired_admin_login_rate_limits",
                side_effect=Exception("skip db"),
            ):
                admin_auth.try_admit_login_attempt(request, settings, username="ghost")

    key = admin_auth.build_source_rate_limit_key(source_result.source)
    assert UNTRUSTED_PEER not in key
    assert "203.0.113" not in key
    combined_logs = " ".join(record.message for record in caplog.records)
    assert "203.0.113" not in combined_logs
    assert "x-forwarded-for" not in combined_logs.lower()


@pytest.mark.integration
def test_uvicorn_forwarded_allow_ips_keeps_untrusted_peer_for_limiter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise deployment-style Uvicorn flags; app resolver must ignore spoofed XFF."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "3")
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    env = os.environ.copy()
    env.pop("DATABASE_URL", None)
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
            TRUSTED_PROXY_CIDRS,
            "--log-level",
            "warning",
        ],
        cwd=REPO_ROOT,
        env=env,
    )
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                response = httpx.get(f"http://127.0.0.1:{port}/health", timeout=1.0)
                if response.status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.2)
        else:
            pytest.fail("uvicorn did not become ready")

        settings = get_settings()
        request = _request(
            peer=UNTRUSTED_PEER,
            headers={"X-Forwarded-For": "203.0.113.60"},
        )
        assert resolve_admin_login_client_source(request, settings).source == UNTRUSTED_PEER
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.mark.integration
def test_login_limiter_blocks_rotating_spoofed_headers_with_trusted_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_admin_auth import FakeRateLimitStore, mock_db_connection, shared_rate_limiter

    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", TRUSTED_PROXY_CIDRS)
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")

    trusted_client = TestClient(app, client=(TRUSTED_PEER, 50000))
    store = FakeRateLimitStore()

    def _post_login(headers: dict[str, str]) -> Any:
        with mock_db_connection():
            form = trusted_client.get("/admin/login")
            csrf = re.search(r'name="csrf_token" value="([^"]+)"', form.text)
            assert csrf is not None
            cookies = {
                admin_auth.LOGIN_FLOW_COOKIE_NAME: form.cookies[admin_auth.LOGIN_FLOW_COOKIE_NAME]
            }
            return trusted_client.post(
                "/admin/login",
                data={
                    "username": "ghost",
                    "password": "wrong-password",
                    "csrf_token": csrf.group(1),
                },
                cookies=cookies,
                headers=headers,
            )

    with shared_rate_limiter(store):
        for index in range(2):
            with mock_db_connection():
                response = _post_login(
                    {"X-Forwarded-For": f"203.0.113.{index}, 198.51.100.80"},
                )
            assert response.status_code == 401

        with mock_db_connection():
            blocked = _post_login({"X-Forwarded-For": "203.0.113.99, 198.51.100.80"})
        assert blocked.status_code == 429
        assert len(store.rows) == 1
