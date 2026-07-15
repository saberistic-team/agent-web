"""Tests for trusted-proxy admin login client source resolution (#239)."""

from __future__ import annotations

import logging
import socket
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any, Callable
from unittest.mock import patch

import pytest
import yaml
from argon2 import PasswordHasher
from fastapi import Request
from starlette.testclient import TestClient

from app import admin_auth
from app.admin_auth import LOGIN_FLOW_COOKIE_NAME
from app.client_source import (
    _resolve_admin_login_client_source,
    normalize_client_address,
    reset_client_source_telemetry_for_tests,
    resolve_admin_login_client_source,
)
from app.proxy_trust import DEFAULT_UVICORN_FORWARDED_ALLOW_IPS
from app.config import get_settings
from app.main import app

REPO_ROOT = Path(__file__).resolve().parents[1]

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "test-session-secret-32chars-minimum"

TRUSTED_RENDER_PEER = "10.0.0.55"
UNTRUSTED_PEER = "198.51.100.10"
CLIENT_IP = "203.0.113.77"
SPOOFED_IP = "203.0.113.99"
ATTACKER_REAL_IP = "198.51.100.42"


def _request_with_peer(
    peer_host: str,
    *,
    headers: list[tuple[bytes, bytes]] | None = None,
) -> Request:
    scope = {
        "type": "http",
        "headers": headers or [],
        "client": (peer_host, 12345),
        "method": "POST",
        "path": "/admin/login",
    }
    return Request(scope)


def _proxy_trust_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv(
        "ADMIN_TRUSTED_PROXY_IPS",
        "10.0.0.0/8,172.16.0.0/12,127.0.0.1",
    )


@pytest.fixture(autouse=True)
def _reset_telemetry() -> None:
    reset_client_source_telemetry_for_tests()
    admin_auth.reset_login_rate_limiter()


@pytest.fixture
def proxy_settings(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    _proxy_trust_env(monkeypatch)
    return get_settings()


def _wrap_app_with_peer(peer_host: str) -> Callable[..., Any]:
    async def middleware(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            scope = {**scope, "client": (peer_host, 0)}
        await app(scope, receive, send)

    return middleware


@pytest.mark.unit
def test_normalize_ipv4_ipv6_and_mapped() -> None:
    assert normalize_client_address("203.0.113.1") == "203.0.113.1"
    assert normalize_client_address("203.0.113.1:8080") == "203.0.113.1"
    assert normalize_client_address("2001:db8::1") == "2001:db8::1"
    assert normalize_client_address("::ffff:203.0.113.50") == "203.0.113.50"
    assert normalize_client_address("[2001:db8::1]:443") == "2001:db8::1"
    assert normalize_client_address("  203.0.113.1  ") == "203.0.113.1"
    assert normalize_client_address("") is None
    assert normalize_client_address("not-an-ip") is None


@pytest.mark.unit
def test_direct_spoof_single_and_multi_value_xff_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _proxy_trust_env(monkeypatch)
    settings = get_settings()
    for header in (
        b"203.0.113.99",
        b"203.0.113.99, 10.0.0.1",
    ):
        request = _request_with_peer(
            UNTRUSTED_PEER,
            headers=[(b"x-forwarded-for", header)],
        )
        assert resolve_admin_login_client_source(request, settings) == UNTRUSTED_PEER
        resolution = _resolve_admin_login_client_source(request, settings)
        assert resolution.path == "untrusted_peer_ignored_headers"


@pytest.mark.unit
def test_cloudflare_append_selects_connecting_not_leftmost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _proxy_trust_env(monkeypatch)
    settings = get_settings()
    request = _request_with_peer(
        TRUSTED_RENDER_PEER,
        headers=[
            (
                b"x-forwarded-for",
                f"{SPOOFED_IP}, {ATTACKER_REAL_IP}".encode("ascii"),
            )
        ],
    )
    assert resolve_admin_login_client_source(request, settings) == ATTACKER_REAL_IP


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _proxy_trust_env(monkeypatch)
    settings = get_settings()
    request = _request_with_peer(
        TRUSTED_RENDER_PEER,
        headers=[(b"x-forwarded-for", CLIENT_IP.encode("ascii"))],
    )
    resolution = _resolve_admin_login_client_source(request, settings)
    assert resolution.source == CLIENT_IP
    assert resolution.path == "xff_trusted_chain"


@pytest.mark.unit
def test_partial_trust_untrusted_intermediary_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _proxy_trust_env(monkeypatch)
    settings = get_settings()
    request = _request_with_peer(
        UNTRUSTED_PEER,
        headers=[
            (b"x-forwarded-for", f"{CLIENT_IP}, {TRUSTED_RENDER_PEER}".encode("ascii")),
            (b"cf-connecting-ip", CLIENT_IP.encode("ascii")),
        ],
    )
    assert resolve_admin_login_client_source(request, settings) == UNTRUSTED_PEER


@pytest.mark.unit
def test_direct_render_origin_ignores_cf_connecting_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _proxy_trust_env(monkeypatch)
    settings = get_settings()
    request = _request_with_peer(
        UNTRUSTED_PEER,
        headers=[(b"cf-connecting-ip", CLIENT_IP.encode("ascii"))],
    )
    assert resolve_admin_login_client_source(request, settings) == UNTRUSTED_PEER


@pytest.mark.unit
def test_header_precedence_cf_over_xff_and_forwarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _proxy_trust_env(monkeypatch)
    settings = get_settings()
    request = _request_with_peer(
        TRUSTED_RENDER_PEER,
        headers=[
            (b"cf-connecting-ip", b"203.0.113.10"),
            (b"x-forwarded-for", b"203.0.113.20"),
            (b"forwarded", b'for=203.0.113.30;proto=https'),
        ],
    )
    assert resolve_admin_login_client_source(request, settings) == "203.0.113.10"

    request_no_cf = _request_with_peer(
        TRUSTED_RENDER_PEER,
        headers=[
            (b"x-forwarded-for", b"203.0.113.20"),
            (b"forwarded", b'for=203.0.113.30;proto=https'),
        ],
    )
    assert resolve_admin_login_client_source(request_no_cf, settings) == "203.0.113.20"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("203.0.113.1", "203.0.113.1"),
        ("2001:db8::9", "2001:db8::9"),
        ("::ffff:10.0.0.1", "10.0.0.1"),
        ("", None),
        ("bogus", None),
    ],
)
def test_address_format_edge_cases(raw: str, expected: str | None) -> None:
    assert normalize_client_address(raw) == expected


@pytest.mark.unit
def test_overlong_xff_chain_falls_back_to_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _proxy_trust_env(monkeypatch)
    settings = get_settings()
    chain = ", ".join(f"203.0.113.{index}" for index in range(12))
    request = _request_with_peer(
        TRUSTED_RENDER_PEER,
        headers=[(b"x-forwarded-for", chain.encode("ascii"))],
    )
    resolution = _resolve_admin_login_client_source(request, settings)
    assert resolution.source == TRUSTED_RENDER_PEER
    assert resolution.path == "chain_too_long"


@pytest.mark.unit
def test_proxy_trust_disabled_uses_peer_even_when_trusted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    settings = get_settings()
    request = _request_with_peer(
        TRUSTED_RENDER_PEER,
        headers=[(b"x-forwarded-for", CLIENT_IP.encode("ascii"))],
    )
    resolution = _resolve_admin_login_client_source(request, settings)
    assert resolution.source == TRUSTED_RENDER_PEER
    assert resolution.path == "direct_peer_proxy_trust_disabled"


@pytest.mark.unit
def test_telemetry_contains_no_raw_addresses(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _proxy_trust_env(monkeypatch)
    settings = get_settings()
    request = _request_with_peer(
        UNTRUSTED_PEER,
        headers=[(b"x-forwarded-for", SPOOFED_IP.encode("ascii"))],
    )
    with caplog.at_level(logging.DEBUG):
        resolve_admin_login_client_source(request, settings)
    for record in caplog.records:
        message = record.getMessage()
        assert SPOOFED_IP not in message
        assert UNTRUSTED_PEER not in message
        if hasattr(record, "admin_login_source_path"):
            assert record.admin_login_source_path == "untrusted_peer_ignored_headers"


@pytest.mark.unit
def test_render_yaml_proxy_trust_configuration_is_consistent() -> None:
    render_yaml = yaml.safe_load((REPO_ROOT / "render.yaml").read_text(encoding="utf-8"))
    service = render_yaml["services"][0]
    start_command = service["startCommand"]
    assert "--forwarded-allow-ips" in start_command

    env = {item["key"]: item.get("value") for item in service["envVars"] if "key" in item}
    assert env["ADMIN_TRUST_PROXY_HEADERS"] == "true"
    trusted_ips = env["ADMIN_TRUSTED_PROXY_IPS"]
    assert "10.0.0.0/8" in trusted_ips

    for cidr in ("10.0.0.0/8", "172.16.0.0/12"):
        assert cidr in start_command

    docs = (REPO_ROOT / "docs" / "ADMIN_AUTH.md").read_text(encoding="utf-8")
    assert "ADMIN_TRUSTED_PROXY_IPS" in docs
    assert "--forwarded-allow-ips" in docs
    assert "CF-Connecting-IP" in docs


@pytest.mark.unit
def test_default_uvicorn_forwarded_allow_ips_matches_trusted_boundary() -> None:
    assert "10.0.0.0/8" in DEFAULT_UVICORN_FORWARDED_ALLOW_IPS
    assert "127.0.0.1" in DEFAULT_UVICORN_FORWARDED_ALLOW_IPS


class _FakeRateLimitStore:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    def try_admit(
        self,
        limiter_keys: tuple[str, ...],
        now: Any,
        *,
        rate_limit: int,
        window_seconds: int,
        lockout_seconds: int,
    ) -> Any:
        from app import db

        for limiter_key in limiter_keys:
            self.rows.setdefault(
                limiter_key,
                {"failure_count": 0, "locked_until": None},
            )
            row = self.rows[limiter_key]
            row["failure_count"] += 1
            if row["failure_count"] >= rate_limit:
                row["locked_until"] = now
                return db.AdminLoginAdmission(
                    admitted=False,
                    throttled=True,
                    already_locked=True,
                    lockout_transition=False,
                )
        return db.AdminLoginAdmission(
            admitted=True,
            throttled=False,
            already_locked=False,
            lockout_transition=False,
        )


def _login_through_peer(
    peer_host: str,
    *,
    headers: dict[str, str] | None = None,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    from tests.test_admin_auth import _extract_csrf_token, mock_db_connection

    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    _proxy_trust_env(monkeypatch)
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "5")

    wrapped = _wrap_app_with_peer(peer_host)
    test_client = TestClient(wrapped, follow_redirects=False)
    store = _FakeRateLimitStore()

    def try_admit(
        conn: Any,
        *,
        limiter_keys: tuple[str, ...],
        now: Any,
        rate_limit: int,
        window_seconds: int,
        lockout_seconds: int,
    ) -> Any:
        return store.try_admit(
            limiter_keys,
            now,
            rate_limit=rate_limit,
            window_seconds=window_seconds,
            lockout_seconds=lockout_seconds,
        )

    with (
        mock_db_connection(),
        patch("app.admin_auth.db.try_admit_admin_login", side_effect=try_admit),
        patch("app.admin_auth.db.cleanup_expired_admin_login_rate_limits", return_value=0),
        patch("app.admin_auth.db.db_connection") as db_conn,
    ):
        db_conn.return_value.__enter__.return_value = object()
        db_conn.return_value.__exit__.return_value = None
        form = test_client.get("/admin/login")
        csrf_token = _extract_csrf_token(form.text)
        cookies = {LOGIN_FLOW_COOKIE_NAME: form.cookies[LOGIN_FLOW_COOKIE_NAME]}
        return test_client.post(
            "/admin/login",
            data={
                "username": "ghost",
                "password": "wrong",
                "csrf_token": csrf_token,
            },
            cookies=cookies,
            headers=headers or {},
        ), store


@pytest.mark.integration
def test_rotating_spoofed_headers_does_not_create_new_limiter_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _proxy_trust_env(monkeypatch)
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "3")
    _, store = _login_through_peer(
        UNTRUSTED_PEER,
        headers={"X-Forwarded-For": "203.0.113.1"},
        monkeypatch=monkeypatch,
    )
    for index in range(2, 6):
        _login_through_peer(
            UNTRUSTED_PEER,
            headers={"X-Forwarded-For": f"203.0.113.{index}"},
            monkeypatch=monkeypatch,
        )
    assert len(store.rows) == 1


@pytest.mark.integration
def test_trusted_proxy_login_uses_resolved_client_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _proxy_trust_env(monkeypatch)
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    _, store = _login_through_peer(
        TRUSTED_RENDER_PEER,
        headers={"X-Forwarded-For": CLIENT_IP},
        monkeypatch=monkeypatch,
    )
    source_key = admin_auth.build_source_rate_limit_key(CLIENT_IP)
    assert source_key in store.rows
    assert len(store.rows) == 1


@pytest.mark.integration
def test_health_reports_proxy_trust_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    _proxy_trust_env(monkeypatch)
    client = TestClient(app)
    payload = client.get("/health").json()
    assert payload["admin_proxy_trust"]["enabled"] is True
    assert payload["admin_proxy_trust"]["trusted_network_count"] >= 3


@pytest.mark.integration
def test_uvicorn_forwarded_allow_ips_integration() -> None:
    """Exercise uvicorn ProxyHeadersMiddleware with the deployment allowlist."""
    script = textwrap.dedent(
        f"""
        import json
        import socket
        import threading
        import time
        import uvicorn
        from fastapi import FastAPI, Request

        app = FastAPI()

        @app.post("/peer")
        def peer(request: Request):
            return {{"peer": request.client.host if request.client else None}}

        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=0,
            log_level="warning",
            forwarded_allow_ips={DEFAULT_UVICORN_FORWARDED_ALLOW_IPS!r},
        )
        server = uvicorn.Server(config)

        def run() -> None:
            server.run()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        while not server.started:
            time.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        assert config.forwarded_allow_ips == {DEFAULT_UVICORN_FORWARDED_ALLOW_IPS!r}
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.connect(("127.0.0.1", port))
            request = (
                "POST /peer HTTP/1.1\\r\\n"
                "Host: 127.0.0.1\\r\\n"
                "X-Forwarded-For: 203.0.113.55\\r\\n"
                "Content-Length: 0\\r\\n\\r\\n"
            )
            sock.sendall(request.encode("ascii"))
            chunks = []
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
                if b"{{" in chunk:
                    break
        server.should_exit = True
        thread.join(timeout=5)
        body = b"".join(chunks).split(b"\\r\\n\\r\\n", 1)[-1]
        payload = json.loads(body.decode("ascii"))
        assert payload["peer"] == "203.0.113.55"
        print("ok")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout
