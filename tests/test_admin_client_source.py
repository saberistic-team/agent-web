"""Tests for trusted-hop admin login client source resolution (#239)."""

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

from app import admin_auth
from app.admin_client_source import (
    ClientSourceResolution,
    normalize_ip_address,
    reset_client_source_telemetry,
    resolve_admin_login_client_source,
)
from app.config import get_settings

REPO_ROOT = Path(__file__).resolve().parents[1]
RENDER_YAML = REPO_ROOT / "render.yaml"
ADMIN_AUTH_DOC = REPO_ROOT / "docs" / "ADMIN_AUTH.md"

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "test-session-secret-32chars-minimum"

RENDER_PROXY = "10.0.0.5"
CLIENT_A = "203.0.113.77"
CLIENT_B = "203.0.113.88"
SPOOFED_LEFT = "198.51.100.99"
UNTRUSTED_PEER = "198.51.100.10"


def _request(
    *,
    peer: str | None,
    headers: dict[str, str] | None = None,
) -> Request:
    scope: dict[str, Any] = {
        "type": "http",
        "headers": [],
        "method": "POST",
        "path": "/admin/login",
    }
    if peer is not None:
        scope["client"] = (peer, 12345)
    if headers:
        scope["headers"] = [
            (name.lower().encode("ascii"), value.encode("ascii"))
            for name, value in headers.items()
        ]
    return Request(scope)


@pytest.fixture(autouse=True)
def _proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    admin_auth.reset_login_rate_limiter()
    reset_client_source_telemetry()


@pytest.mark.unit
def test_normalize_ip_address_formats() -> None:
    assert normalize_ip_address("203.0.113.1") == "203.0.113.1"
    assert normalize_ip_address("203.0.113.1:8080") == "203.0.113.1"
    assert normalize_ip_address("2001:db8::1") == "2001:db8::1"
    assert normalize_ip_address("::ffff:203.0.113.50") == "203.0.113.50"
    assert normalize_ip_address("[2001:db8::1]:443") == "2001:db8::1"
    assert normalize_ip_address("  203.0.113.1  ") == "203.0.113.1"
    assert normalize_ip_address("") is None
    assert normalize_ip_address("not-an-ip") is None


@pytest.mark.unit
def test_direct_spoof_single_and_multi_value_xff_ignored() -> None:
    settings = get_settings()
    for header in (
        SPOOFED_LEFT,
        f"{SPOOFED_LEFT}, {CLIENT_A}",
        f"{SPOOFED_LEFT}, {CLIENT_A}, {RENDER_PROXY}",
    ):
        request = _request(
            peer=UNTRUSTED_PEER,
            headers={"X-Forwarded-For": header, "CF-Connecting-IP": CLIENT_A},
        )
        resolution = resolve_admin_login_client_source(request, settings)
        assert resolution == ClientSourceResolution(
            address=UNTRUSTED_PEER,
            path="direct_peer",
        )


@pytest.mark.unit
def test_cloudflare_append_selects_appended_client_not_leftmost() -> None:
    settings = get_settings()
    request = _request(
        peer=RENDER_PROXY,
        headers={"X-Forwarded-For": f"{SPOOFED_LEFT}, {CLIENT_A}"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.address == CLIENT_A
    assert resolution.path == "trusted_x_forwarded_for"


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client() -> None:
    settings = get_settings()
    request = _request(
        peer=RENDER_PROXY,
        headers={"X-Forwarded-For": f"{CLIENT_A}, 172.16.0.2"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.address == CLIENT_A
    assert resolution.path == "trusted_x_forwarded_for"


@pytest.mark.unit
def test_partial_trust_untrusted_intermediary_fails_closed_to_peer() -> None:
    settings = get_settings()
    request = _request(
        peer=UNTRUSTED_PEER,
        headers={"X-Forwarded-For": f"{CLIENT_A}, {RENDER_PROXY}"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.address == UNTRUSTED_PEER
    assert resolution.path == "direct_peer"


@pytest.mark.unit
def test_direct_render_origin_ignores_cf_connecting_ip() -> None:
    settings = get_settings()
    request = _request(
        peer=UNTRUSTED_PEER,
        headers={"CF-Connecting-IP": CLIENT_A, "X-Forwarded-For": SPOOFED_LEFT},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.address == UNTRUSTED_PEER
    assert resolution.path == "direct_peer"


@pytest.mark.unit
def test_header_precedence_xff_before_forwarded_before_cf() -> None:
    settings = get_settings()
    request = _request(
        peer=RENDER_PROXY,
        headers={
            "X-Forwarded-For": f"{SPOOFED_LEFT}, {CLIENT_A}",
            "Forwarded": f'for="{CLIENT_B}"',
            "CF-Connecting-IP": "203.0.113.1",
        },
    )
    assert resolve_admin_login_client_source(request, settings).address == CLIENT_A

    request = _request(
        peer=RENDER_PROXY,
        headers={
            "Forwarded": f'for="{CLIENT_B}"',
            "CF-Connecting-IP": "203.0.113.1",
        },
    )
    assert resolve_admin_login_client_source(request, settings).address == CLIENT_B

    request = _request(
        peer=RENDER_PROXY,
        headers={"CF-Connecting-IP": CLIENT_A},
    )
    assert resolve_admin_login_client_source(request, settings).address == CLIENT_A
    assert (
        resolve_admin_login_client_source(request, settings).path
        == "trusted_cf_connecting_ip"
    )


@pytest.mark.unit
def test_invalid_and_overlong_forwarding_fail_closed() -> None:
    settings = get_settings()
    overlong = ", ".join([f"10.0.0.{index}" for index in range(1, 25)])
    request = _request(peer=RENDER_PROXY, headers={"X-Forwarded-For": overlong})
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.address == "unknown"
    assert resolution.path == "invalid_forwarding"

    request = _request(
        peer=RENDER_PROXY,
        headers={"X-Forwarded-For": "not-an-ip, also-bad"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.address == "unknown"
    assert resolution.path == "invalid_forwarding"


@pytest.mark.unit
def test_whitespace_and_empty_elements_are_skipped_deterministically() -> None:
    settings = get_settings()
    request = _request(
        peer=RENDER_PROXY,
        headers={"X-Forwarded-For": f"  {SPOOFED_LEFT}  ,   , {CLIENT_A} "},
    )
    assert resolve_admin_login_client_source(request, settings).address == CLIENT_A


@pytest.mark.unit
def test_rotating_spoofed_headers_do_not_create_new_limiter_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "3")
    settings = get_settings()
    keys: set[str] = set()
    for index in range(5):
        request = _request(
            peer=RENDER_PROXY,
            headers={"X-Forwarded-For": f"203.0.113.{index}, {CLIENT_A}"},
        )
        source = admin_auth.client_ip(request, settings)
        keys.add(admin_auth.build_source_rate_limit_key(source))
    assert keys == {admin_auth.build_source_rate_limit_key(CLIENT_A)}


@pytest.mark.unit
def test_telemetry_logs_path_without_raw_addresses(
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = get_settings()
    caplog.set_level(logging.INFO)
    reset_client_source_telemetry()

    request = _request(
        peer=UNTRUSTED_PEER,
        headers={"X-Forwarded-For": f"{SPOOFED_LEFT}, {CLIENT_A}"},
    )
    resolve_admin_login_client_source(request, settings)

    assert any(
        record.message == "Admin login client source used conservative path"
        for record in caplog.records
    )
    for record in caplog.records:
        message = record.getMessage()
        assert SPOOFED_LEFT not in message
        assert CLIENT_A not in message
        assert UNTRUSTED_PEER not in message
        extra = getattr(record, "client_source_path", None)
        if extra is not None:
            assert extra in {
                "direct_peer",
                "trusted_x_forwarded_for",
                "trusted_forwarded",
                "trusted_cf_connecting_ip",
                "invalid_forwarding",
                "missing_peer",
                "invalid_peer",
            }


@pytest.mark.unit
def test_deployment_configuration_is_consistent() -> None:
    render_text = RENDER_YAML.read_text(encoding="utf-8")
    doc_text = ADMIN_AUTH_DOC.read_text(encoding="utf-8")

    assert "--forwarded-allow-ips=" in render_text
    assert "ADMIN_TRUST_PROXY_HEADERS" in render_text
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in render_text
    assert "10.0.0.0/8" in render_text
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in doc_text
    assert "right-to-left" in doc_text.lower() or "right to left" in doc_text.lower()

    start_match = re.search(r"startCommand:\s*(.+)", render_text)
    assert start_match is not None
    start_command = start_match.group(1)
    assert "uvicorn" in start_command
    assert "--forwarded-allow-ips=" in start_command
    assert "--proxy-headers" not in start_command


@pytest.mark.unit
def test_privacy_limiter_keys_and_logs_exclude_raw_forwarding(
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = get_settings()
    caplog.set_level(logging.INFO)
    request = _request(
        peer=RENDER_PROXY,
        headers={"X-Forwarded-For": f"{SPOOFED_LEFT}, {CLIENT_A}"},
    )
    with patch("app.admin_auth.db.db_connection", side_effect=Exception("offline")):
        admin_auth.try_admit_login_attempt(request, settings, username="ghost")

    for record in caplog.records:
        rendered = str(record.__dict__)
        assert SPOOFED_LEFT not in rendered
        assert "x-forwarded-for" not in rendered.lower()

    source_key = admin_auth.build_source_rate_limit_key(CLIENT_A)
    assert SPOOFED_LEFT not in source_key
    assert CLIENT_A not in source_key
    assert len(source_key) == 64


@pytest.mark.integration
def test_try_admit_login_collapses_rotating_spoofed_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resolver + limiter integration: rotating leftmost XFF values share one source bucket."""
    from datetime import datetime, timezone

    from app import db as db_module

    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", "10.0.0.0/8")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "3")
    settings = get_settings()
    store: dict[str, dict[str, Any]] = {}
    now = datetime.now(timezone.utc)

    def try_admit(
        conn: Any,
        *,
        limiter_keys: tuple[str, ...],
        now: datetime,
        rate_limit: int,
        window_seconds: int,
        lockout_seconds: int,
    ) -> db_module.AdminLoginAdmission:
        key = limiter_keys[0]
        row = store.setdefault(
            key,
            {"failure_count": 0, "window_started_at": now, "locked_until": None},
        )
        row["failure_count"] += 1
        return db_module.AdminLoginAdmission(
            admitted=True,
            throttled=False,
            already_locked=False,
            lockout_transition=row["failure_count"] >= rate_limit,
        )

    with (
        patch("app.admin_auth.db.try_admit_admin_login", side_effect=try_admit),
        patch("app.admin_auth.db.cleanup_expired_admin_login_rate_limits", return_value=0),
        patch("app.admin_auth.db.db_connection") as db_conn,
    ):
        db_conn.return_value.__enter__.return_value = object()
        db_conn.return_value.__exit__.return_value = None

        keys: set[str] = set()
        for index in range(4):
            request = _request(
                peer=RENDER_PROXY,
                headers={"X-Forwarded-For": f"203.0.113.{index}, {CLIENT_A}"},
            )
            source = admin_auth.client_ip(request, settings)
            admin_auth.try_admit_login_attempt(request, settings, username="ghost")
            keys.add(admin_auth.build_source_rate_limit_key(source))

    assert keys == {admin_auth.build_source_rate_limit_key(CLIENT_A)}
    assert len(store) == 1


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.mark.integration
def test_uvicorn_start_command_declared_in_render_yaml() -> None:
    """Boot uvicorn with the same forwarded-allow-ips boundary declared in render.yaml."""
    render_text = RENDER_YAML.read_text(encoding="utf-8")
    start_match = re.search(r"startCommand:\s*(.+)", render_text)
    assert start_match is not None
    start_command = start_match.group(1).strip()
    assert "--forwarded-allow-ips=" in start_command

    port = _free_port()
    env = os.environ.copy()
    env.pop("DATABASE_URL", None)
    env["BASE_URL"] = f"http://127.0.0.1:{port}"

    proc = subprocess.Popen(
        start_command.replace("$PORT", str(port)).split(),
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 20.0
        origin = f"http://127.0.0.1:{port}"
        while time.monotonic() < deadline:
            try:
                health = httpx.get(f"{origin}/health", timeout=1.0)
                if health.status_code == 200 and health.json().get("status") == "ok":
                    break
            except httpx.HTTPError:
                time.sleep(0.2)
        else:
            raise AssertionError("uvicorn did not become ready with render.yaml start command")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
