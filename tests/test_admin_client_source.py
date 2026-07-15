"""Tests for trusted-proxy admin login client source resolution (#239)."""

from __future__ import annotations

import logging
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from argon2 import PasswordHasher
from fastapi import Request

from app import admin_auth
from app.admin_client_source import (
    SourceResolutionPath,
    normalize_ip_literal,
    parse_trusted_proxy_networks,
    reset_untrusted_forwarding_telemetry_for_tests,
    resolve_admin_login_client_source,
)
from app.config import get_settings

REPO_ROOT = Path(__file__).resolve().parent.parent
RENDER_TRUSTED_PROXIES = "10.0.0.0/8,127.0.0.1"
RENDER_PROXY = "10.0.0.1"
REAL_CLIENT = "203.0.113.77"
SPOOFED_CLIENT = "203.0.113.99"
CLOUDFLARE_EDGE = "198.51.100.10"
DIRECT_PEER = "198.51.100.20"


def _request_with_client(
    host: str,
    *,
    headers: list[tuple[bytes, bytes]] | None = None,
) -> Request:
    scope: dict[str, Any] = {
        "type": "http",
        "headers": headers or [],
        "client": (host, 12345),
        "method": "POST",
        "path": "/admin/login",
    }
    return Request(scope)


def _settings(monkeypatch: pytest.MonkeyPatch, *, trusted: str = RENDER_TRUSTED_PROXIES) -> Any:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", trusted)
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    return get_settings()


@pytest.fixture(autouse=True)
def reset_telemetry() -> None:
    reset_untrusted_forwarding_telemetry_for_tests()


@pytest.mark.unit
def test_normalize_ip_literal_formats() -> None:
    assert normalize_ip_literal("203.0.113.1") == "203.0.113.1"
    assert normalize_ip_literal(" 2001:db8::1 ") == "2001:db8::1"
    assert normalize_ip_literal("::ffff:203.0.113.50") == "203.0.113.50"
    assert normalize_ip_literal("203.0.113.1:443") == "203.0.113.1"
    assert normalize_ip_literal("[2001:db8::1]:443") == "2001:db8::1"
    assert normalize_ip_literal("") is None
    assert normalize_ip_literal("not-an-ip") is None


@pytest.mark.unit
def test_direct_spoof_single_and_multi_value_xff_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, trusted="")
    for xff in (
        SPOOFED_CLIENT,
        f"{SPOOFED_CLIENT}, {REAL_CLIENT}",
        f"{SPOOFED_CLIENT}, {REAL_CLIENT}, {RENDER_PROXY}",
    ):
        request = _request_with_client(
            DIRECT_PEER,
            headers=[(b"x-forwarded-for", xff.encode("ascii"))],
        )
        result = resolve_admin_login_client_source(request, settings)
        assert result.source == DIRECT_PEER
        assert result.path is SourceResolutionPath.DIRECT_PEER


@pytest.mark.unit
def test_cloudflare_append_ignores_attacker_leftmost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    xff = f"{SPOOFED_CLIENT}, {REAL_CLIENT}"
    request = _request_with_client(
        RENDER_PROXY,
        headers=[(b"x-forwarded-for", xff.encode("ascii"))],
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.source == REAL_CLIENT
    assert result.path is SourceResolutionPath.XFF_TRUSTED_CHAIN


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, trusted=f"{RENDER_TRUSTED_PROXIES},{CLOUDFLARE_EDGE}")
    xff = f"{REAL_CLIENT}, {CLOUDFLARE_EDGE}"
    request = _request_with_client(
        RENDER_PROXY,
        headers=[(b"x-forwarded-for", xff.encode("ascii"))],
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.source == REAL_CLIENT
    assert result.path is SourceResolutionPath.XFF_TRUSTED_CHAIN


@pytest.mark.unit
def test_partial_trust_untrusted_intermediary_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, trusted=RENDER_TRUSTED_PROXIES)
    untrusted_intermediary = "203.0.113.5"
    request = _request_with_client(
        untrusted_intermediary,
        headers=[
            (b"x-forwarded-for", f"{REAL_CLIENT}, {RENDER_PROXY}".encode("ascii")),
            (b"cf-connecting-ip", REAL_CLIENT.encode("ascii")),
        ],
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.source == untrusted_intermediary
    assert result.path is SourceResolutionPath.DIRECT_PEER


@pytest.mark.unit
def test_direct_render_origin_ignores_spoofed_cloudflare_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    request = _request_with_client(
        DIRECT_PEER,
        headers=[
            (b"cf-connecting-ip", SPOOFED_CLIENT.encode("ascii")),
            (b"x-forwarded-for", f"{SPOOFED_CLIENT}, {RENDER_PROXY}".encode("ascii")),
        ],
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.source == DIRECT_PEER
    assert result.path is SourceResolutionPath.DIRECT_PEER


@pytest.mark.unit
def test_header_precedence_cf_over_conflicting_xff_and_forwarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    request = _request_with_client(
        RENDER_PROXY,
        headers=[
            (b"x-forwarded-for", f"{SPOOFED_CLIENT}, {REAL_CLIENT}".encode("ascii")),
            (b"cf-connecting-ip", REAL_CLIENT.encode("ascii")),
            (b"forwarded", f'for={SPOOFED_CLIENT};proto=https'.encode("ascii")),
        ],
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.source == REAL_CLIENT
    assert result.path is SourceResolutionPath.CF_CONNECTING_IP


@pytest.mark.unit
def test_forwarded_header_used_when_cf_and_xff_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    request = _request_with_client(
        RENDER_PROXY,
        headers=[(b"forwarded", f'for="{REAL_CLIENT}";proto=https'.encode("ascii"))],
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.source == REAL_CLIENT
    assert result.path is SourceResolutionPath.FORWARDED_HEADER


@pytest.mark.unit
@pytest.mark.parametrize(
    ("xff", "expected_path"),
    [
        ("", SourceResolutionPath.DIRECT_PEER),
        (" , ", SourceResolutionPath.MALFORMED_FALLBACK),
        (",".join(["203.0.113.1"] * 40), SourceResolutionPath.MALFORMED_FALLBACK),
        (f"{RENDER_PROXY}, {CLOUDFLARE_EDGE}", SourceResolutionPath.ALL_TRUSTED_FALLBACK),
    ],
)
def test_malformed_and_overlong_chains_are_conservative(
    monkeypatch: pytest.MonkeyPatch,
    xff: str,
    expected_path: SourceResolutionPath,
) -> None:
    settings = _settings(monkeypatch, trusted=f"{RENDER_TRUSTED_PROXIES},{CLOUDFLARE_EDGE}")
    headers = [(b"x-forwarded-for", xff.encode("ascii"))] if xff else []
    request = _request_with_client(RENDER_PROXY, headers=headers)
    result = resolve_admin_login_client_source(request, settings)
    assert result.path is expected_path
    assert result.source in {RENDER_PROXY, "unknown"}


@pytest.mark.unit
def test_missing_peer_returns_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch)
    scope = {
        "type": "http",
        "headers": [],
        "client": None,
        "method": "POST",
        "path": "/admin/login",
    }
    result = resolve_admin_login_client_source(Request(scope), settings)
    assert result.source == "unknown"
    assert result.path is SourceResolutionPath.MISSING_PEER


@pytest.mark.unit
def test_untrusted_forwarding_attempt_emits_sampled_telemetry(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr("app.admin_client_source.random.random", lambda: 0.0)
    settings = _settings(monkeypatch, trusted="")
    request = _request_with_client(
        DIRECT_PEER,
        headers=[(b"x-forwarded-for", SPOOFED_CLIENT.encode("ascii"))],
    )
    with caplog.at_level(logging.INFO, logger="app.admin_client_source"):
        resolve_admin_login_client_source(request, settings)
    assert any(
        "ignored untrusted forwarding headers" in record.message
        for record in caplog.records
    )
    assert all(SPOOFED_CLIENT not in record.message for record in caplog.records)
    assert all(DIRECT_PEER not in record.message for record in caplog.records)


@pytest.mark.unit
def test_telemetry_and_logs_exclude_raw_forwarding_data(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = _settings(monkeypatch)
    request = _request_with_client(
        RENDER_PROXY,
        headers=[
            (b"x-forwarded-for", f"{SPOOFED_CLIENT}, {REAL_CLIENT}".encode("ascii")),
            (b"cf-connecting-ip", REAL_CLIENT.encode("ascii")),
        ],
    )
    with caplog.at_level(logging.DEBUG, logger="app.admin_client_source"):
        resolve_admin_login_client_source(request, settings)
    joined = "\n".join(record.message for record in caplog.records)
    assert REAL_CLIENT not in joined
    assert SPOOFED_CLIENT not in joined
    assert "x-forwarded-for" not in joined.lower()


@pytest.mark.unit
def test_parse_trusted_proxy_networks_accepts_cidr_and_host() -> None:
    networks = parse_trusted_proxy_networks("10.0.0.0/8, 127.0.0.1")
    assert len(networks) == 2


@pytest.mark.unit
def test_client_ip_wrapper_delegates_to_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    request = _request_with_client(
        RENDER_PROXY,
        headers=[(b"cf-connecting-ip", REAL_CLIENT.encode("ascii"))],
    )
    assert admin_auth.client_ip(request, settings) == REAL_CLIENT


@pytest.mark.unit
def test_rotating_spoofed_headers_do_not_create_distinct_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, trusted="")
    keys = {
        admin_auth.build_source_rate_limit_key(
            resolve_admin_login_client_source(
                _request_with_client(
                    DIRECT_PEER,
                    headers=[(b"x-forwarded-for", f"203.0.113.{index}".encode("ascii"))],
                ),
                settings,
            ).source
        )
        for index in range(6)
    }
    assert len(keys) == 1


@pytest.mark.unit
def test_render_yaml_proxy_settings_are_consistent() -> None:
    render_text = (REPO_ROOT / "render.yaml").read_text(encoding="utf-8")
    assert "startCommand: uvicorn app.main:app --host 0.0.0.0 --port $PORT --forwarded-allow-ips ''" in render_text
    assert 'key: ADMIN_TRUSTED_PROXY_IPS' in render_text
    assert 'value: "10.0.0.0/8,127.0.0.1"' in render_text
    assert 'key: UVICORN_FORWARDED_ALLOW_IPS' in render_text
    assert 'value: ""' in render_text
    assert "ADMIN_TRUST_PROXY_HEADERS" not in render_text


@pytest.mark.unit
def test_admin_auth_docs_document_trust_model() -> None:
    docs = (REPO_ROOT / "docs" / "ADMIN_AUTH.md").read_text(encoding="utf-8")
    assert "ADMIN_TRUSTED_PROXY_IPS" in docs
    assert "ADMIN_TRUST_PROXY_HEADERS" not in docs
    assert "right-to-left" in docs.lower()
    assert "--forwarded-allow-ips" in docs


@pytest.mark.integration
def test_uvicorn_start_command_rejects_forwarded_spoof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the deployment Uvicorn forwarded-allow-ips setting over real HTTP."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("ADMIN_USERNAME", "operator")
    monkeypatch.setenv(
        "ADMIN_PASSWORD_HASH",
        PasswordHasher().hash("integration-password"),
    )
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "integration-secret-32chars-minimum")
    monkeypatch.setenv("BASE_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", "")
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)

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
            "",
            "--log-level",
            "warning",
        ],
        cwd=REPO_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                with httpx.Client() as client:
                    if client.get(f"http://127.0.0.1:{port}/health", timeout=1).status_code == 200:
                        break
            except httpx.HTTPError:
                time.sleep(0.2)
        else:
            pytest.fail("uvicorn did not become ready")

        with httpx.Client(follow_redirects=False) as client:
            form = client.get(f"http://127.0.0.1:{port}/admin/login")
            csrf = _extract_csrf(form.text)

            for spoofed in ("203.0.113.1", "203.0.113.2"):
                response = client.post(
                    f"http://127.0.0.1:{port}/admin/login",
                    data={
                        "username": "operator",
                        "password": "wrong-password",
                        "csrf_token": csrf,
                    },
                    headers={"X-Forwarded-For": spoofed},
                )
                # Admission is counted before login-flow persistence; preview mode has no DB.
                assert response.status_code in {400, 401, 500}
                if response.status_code != 500:
                    csrf = _extract_csrf(response.text)

            blocked = client.post(
                f"http://127.0.0.1:{port}/admin/login",
                data={
                    "username": "operator",
                    "password": "wrong-password",
                    "csrf_token": csrf,
                },
                headers={"X-Forwarded-For": "203.0.113.3"},
            )
            assert blocked.status_code == 429
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _extract_csrf(html: str) -> str:
    marker = 'name="csrf_token" value="'
    start = html.index(marker) + len(marker)
    end = html.index('"', start)
    return html[start:end]
