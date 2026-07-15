"""Tests for trusted-hop admin login client source resolution."""

from __future__ import annotations

import logging
import socket
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest
import uvicorn
from argon2 import PasswordHasher
from fastapi import Request
from fastapi.testclient import TestClient

from app import admin_auth
from app.admin_client_source import (
    PRODUCTION_FORWARDED_ALLOW_IPS,
    PRODUCTION_TRUSTED_PROXY_IPS,
    PRODUCTION_UVICORN_START_COMMAND,
    ClientSourceResolution,
    _resolve_admin_login_client_source,
    normalize_client_address,
    resolve_admin_login_client_source,
)
from app.config import get_settings
from app.main import app

REPO_ROOT = Path(__file__).resolve().parents[1]
RENDER_YAML = REPO_ROOT / "render.yaml"
ADMIN_AUTH_DOC = REPO_ROOT / "docs" / "ADMIN_AUTH.md"

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "test-session-secret-32chars-minimum"

TRUSTED_PROXY_PEER = "10.0.0.5"
RENDER_LB_PEER = "10.1.2.3"
REAL_CLIENT = "203.0.113.77"
SPOOFED_CLIENT = "203.0.113.99"
OTHER_CLIENT = "203.0.113.88"


@pytest.fixture(autouse=True)
def admin_source_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests import test_admin_auth as admin_auth_tests

    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    admin_auth.reset_login_rate_limiter()
    admin_auth_tests._login_flows.clear()
    admin_auth_tests._session_store.clear()


def _request(
    *,
    peer: str,
    headers: dict[str, str] | None = None,
) -> Request:
    scope: dict[str, Any] = {
        "type": "http",
        "headers": [
            (key.lower().encode("latin-1"), value.encode("latin-1"))
            for key, value in (headers or {}).items()
        ],
        "client": (peer, 12345),
        "method": "POST",
        "path": "/admin/login",
    }
    return Request(scope)


@pytest.fixture
def trusted_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", "10.0.0.0/8,127.0.0.1")


@pytest.fixture
def trusted_login_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", "10.0.0.0/8")
    return TestClient(app, client=(TRUSTED_PROXY_PEER, 50000), follow_redirects=False)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("203.0.113.1", "203.0.113.1"),
        (" 203.0.113.1 ", "203.0.113.1"),
        ("203.0.113.1:443", "203.0.113.1"),
        ("[2001:db8::1]", "2001:db8::1"),
        ("[2001:db8::1]:443", "2001:db8::1"),
        ("::ffff:203.0.113.5", "203.0.113.5"),
        ("", None),
        ("not-an-ip", None),
        ("203.0.113.1:notaport", None),
    ],
)
def test_normalize_client_address(raw: str, expected: str | None) -> None:
    assert normalize_client_address(raw) == expected


@pytest.mark.unit
def test_direct_spoof_single_and_multi_xff_ignored_without_trusted_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    settings = get_settings()
    direct_peer = "198.51.100.10"

    single = _request(
        peer=direct_peer,
        headers={"X-Forwarded-For": SPOOFED_CLIENT},
    )
    multi = _request(
        peer=direct_peer,
        headers={"X-Forwarded-For": f"{SPOOFED_CLIENT}, {OTHER_CLIENT}"},
    )

    assert resolve_admin_login_client_source(single, settings) == direct_peer
    assert resolve_admin_login_client_source(multi, settings) == direct_peer


@pytest.mark.unit
def test_cloudflare_append_selects_connecting_client_not_leftmost(
    trusted_proxy_env: None,
) -> None:
    settings = get_settings()
    request = _request(
        peer=RENDER_LB_PEER,
        headers={
            "X-Forwarded-For": f"{SPOOFED_CLIENT}, {REAL_CLIENT}",
            "CF-Connecting-IP": REAL_CLIENT,
        },
    )
    assert resolve_admin_login_client_source(request, settings) == REAL_CLIENT


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client_from_xff(
    trusted_proxy_env: None,
) -> None:
    settings = get_settings()
    request = _request(
        peer=RENDER_LB_PEER,
        headers={"X-Forwarded-For": f"{REAL_CLIENT}, {RENDER_LB_PEER}"},
    )
    assert resolve_admin_login_client_source(request, settings) == REAL_CLIENT


@pytest.mark.unit
def test_partial_trust_fails_closed_behind_untrusted_intermediary(
    trusted_proxy_env: None,
) -> None:
    settings = get_settings()
    untrusted_peer = "203.0.113.50"
    request = _request(
        peer=untrusted_peer,
        headers={
            "X-Forwarded-For": f"{REAL_CLIENT}, {RENDER_LB_PEER}",
            "CF-Connecting-IP": SPOOFED_CLIENT,
        },
    )
    assert resolve_admin_login_client_source(request, settings) == untrusted_peer


@pytest.mark.unit
def test_direct_render_origin_ignores_cloudflare_vendor_headers(
    trusted_proxy_env: None,
) -> None:
    settings = get_settings()
    direct_peer = "198.51.100.44"
    request = _request(
        peer=direct_peer,
        headers={
            "CF-Connecting-IP": SPOOFED_CLIENT,
            "X-Forwarded-For": SPOOFED_CLIENT,
            "Forwarded": f'for={SPOOFED_CLIENT};proto=https',
        },
    )
    assert resolve_admin_login_client_source(request, settings) == direct_peer


@pytest.mark.unit
def test_header_precedence_cf_over_conflicting_xff_and_forwarded(
    trusted_proxy_env: None,
) -> None:
    settings = get_settings()
    request = _request(
        peer=RENDER_LB_PEER,
        headers={
            "CF-Connecting-IP": REAL_CLIENT,
            "X-Forwarded-For": f"{SPOOFED_CLIENT}, {OTHER_CLIENT}",
            "Forwarded": f'for="{SPOOFED_CLIENT}";proto=https',
        },
    )
    assert resolve_admin_login_client_source(request, settings) == REAL_CLIENT


@pytest.mark.unit
def test_xff_used_when_cf_missing_with_right_to_left_walk(
    trusted_proxy_env: None,
) -> None:
    settings = get_settings()
    request = _request(
        peer=RENDER_LB_PEER,
        headers={"X-Forwarded-For": f"{SPOOFED_CLIENT}, {REAL_CLIENT}"},
    )
    assert resolve_admin_login_client_source(request, settings) == REAL_CLIENT


@pytest.mark.unit
def test_forwarded_header_right_to_left_when_xff_missing(
    trusted_proxy_env: None,
) -> None:
    settings = get_settings()
    request = _request(
        peer=RENDER_LB_PEER,
        headers={
            "Forwarded": (
                f'for={SPOOFED_CLIENT};proto=https, '
                f'for="{REAL_CLIENT}";proto=https, '
                f'for={RENDER_LB_PEER};proto=https'
            )
        },
    )
    assert resolve_admin_login_client_source(request, settings) == REAL_CLIENT


@pytest.mark.unit
def test_overlong_xff_chain_resolves_unknown(trusted_proxy_env: None) -> None:
    settings = get_settings()
    hops = ", ".join(f"10.0.0.{index}" for index in range(40))
    request = _request(peer=RENDER_LB_PEER, headers={"X-Forwarded-For": hops})
    assert resolve_admin_login_client_source(request, settings) == "unknown"


@pytest.mark.unit
def test_invalid_xff_entries_skip_to_next_untrusted_hop(
    trusted_proxy_env: None,
) -> None:
    settings = get_settings()
    request = _request(
        peer=RENDER_LB_PEER,
        headers={"X-Forwarded-For": f"not-an-ip, {REAL_CLIENT}, {RENDER_LB_PEER}"},
    )
    assert resolve_admin_login_client_source(request, settings) == REAL_CLIENT


@pytest.mark.unit
def test_missing_peer_resolves_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    settings = get_settings()
    scope = {
        "type": "http",
        "headers": [],
        "client": None,
        "method": "POST",
        "path": "/admin/login",
    }
    request = Request(scope)
    assert resolve_admin_login_client_source(request, settings) == "unknown"


@pytest.mark.unit
def test_telemetry_path_labels_without_raw_addresses(
    trusted_proxy_env: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = get_settings()
    request = _request(
        peer=RENDER_LB_PEER,
        headers={"CF-Connecting-IP": REAL_CLIENT},
    )
    with caplog.at_level(logging.DEBUG):
        resolve_admin_login_client_source(request, settings)
    assert REAL_CLIENT not in caplog.text
    assert any(
        getattr(record, "admin_client_source_path", None) == "cf_connecting_ip"
        for record in caplog.records
    )


@pytest.mark.unit
def test_rejected_untrusted_forwarding_is_sampled(
    trusted_proxy_env: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from app.admin_client_source import reset_source_resolution_telemetry

    reset_source_resolution_telemetry()
    settings = get_settings()
    request = _request(
        peer="203.0.113.60",
        headers={"X-Forwarded-For": SPOOFED_CLIENT},
    )
    with caplog.at_level(logging.INFO):
        resolve_admin_login_client_source(request, settings)
    assert SPOOFED_CLIENT not in caplog.text
    assert any(
        record.message == "Admin login ignored untrusted forwarding headers"
        for record in caplog.records
    )


@pytest.mark.unit
def test_resolution_dataclass_for_tests(trusted_proxy_env: None) -> None:
    settings = get_settings()
    resolution = _resolve_admin_login_client_source(
        _request(peer=RENDER_LB_PEER, headers={"CF-Connecting-IP": REAL_CLIENT}),
        settings,
    )
    assert resolution == ClientSourceResolution(
        source=REAL_CLIENT,
        path="cf_connecting_ip",
    )


@pytest.mark.unit
@pytest.mark.integration
def test_rotating_spoofed_headers_do_not_create_new_limiter_rows(
    trusted_proxy_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_admin_auth import (
        FakeRateLimitStore,
        mock_db_connection,
        shared_rate_limiter,
    )

    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    store = FakeRateLimitStore()
    login_client = TestClient(app, client=(TRUSTED_PROXY_PEER, 50000), follow_redirects=False)

    def _trusted_login(headers: dict[str, str]) -> Any:
        with mock_db_connection():
            form = login_client.get("/admin/login")
            csrf_token, cookies = _parse_login_form(form)
            return login_client.post(
                "/admin/login",
                data={
                    "username": "ghost",
                    "password": "wrong-password",
                    "csrf_token": csrf_token,
                },
                cookies=cookies,
                headers=headers,
            )

    with shared_rate_limiter(store):
        assert _trusted_login({"X-Forwarded-For": f"{SPOOFED_CLIENT}, {REAL_CLIENT}"}).status_code == 401
        assert _trusted_login({"X-Forwarded-For": f"{OTHER_CLIENT}, {REAL_CLIENT}"}).status_code == 401
        blocked = _trusted_login({"X-Forwarded-For": f"203.0.113.1, {REAL_CLIENT}"})
        assert blocked.status_code == 429
        source_key = admin_auth.build_source_rate_limit_key(REAL_CLIENT)
        assert list(store.rows.keys()) == [source_key]


def _parse_login_form(response: Any) -> tuple[str, dict[str, str]]:
    import re

    match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert match is not None
    csrf_token = match.group(1)
    cookies = {
        admin_auth.LOGIN_FLOW_COOKIE_NAME: response.cookies.get(admin_auth.LOGIN_FLOW_COOKIE_NAME) or ""
    }
    return csrf_token, cookies


@pytest.mark.unit
def test_deployment_proxy_settings_are_consistent() -> None:
    render_text = RENDER_YAML.read_text(encoding="utf-8")
    doc_text = ADMIN_AUTH_DOC.read_text(encoding="utf-8")

    assert PRODUCTION_FORWARDED_ALLOW_IPS in render_text
    assert "ADMIN_TRUST_PROXY_HEADERS" in render_text
    assert PRODUCTION_TRUSTED_PROXY_IPS in render_text
    assert PRODUCTION_UVICORN_START_COMMAND.split("--forwarded-allow-ips")[0] in render_text
    assert "--forwarded-allow-ips" in doc_text
    assert "right-to-left" in doc_text
    assert "ADMIN_TRUSTED_PROXY_IPS" in doc_text


@pytest.mark.unit
def test_limiter_keys_and_logs_contain_no_raw_forwarding_data(
    trusted_proxy_env: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from tests.test_admin_auth import FakeRateLimitStore, shared_rate_limiter

    settings = get_settings()
    request = _request(
        peer=RENDER_LB_PEER,
        headers={
            "X-Forwarded-For": f"{SPOOFED_CLIENT}, {REAL_CLIENT}",
            "CF-Connecting-IP": REAL_CLIENT,
        },
    )
    source = resolve_admin_login_client_source(request, settings)
    key = admin_auth.build_source_rate_limit_key(source)
    store = FakeRateLimitStore()
    with shared_rate_limiter(store), caplog.at_level(logging.DEBUG):
        admin_auth.try_admit_login_attempt(
            request,
            settings,
            username=TEST_USERNAME,
        )
    assert SPOOFED_CLIENT not in key
    assert REAL_CLIENT not in key
    assert SPOOFED_CLIENT not in caplog.text
    assert "x-forwarded-for" not in caplog.text.lower()


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.mark.integration
def test_uvicorn_proxy_configuration_matches_deployment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the same uvicorn forwarded-allow-ips boundary as render.yaml."""
    from tests.test_admin_auth import FakeRateLimitStore, mock_db_connection, shared_rate_limiter

    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("BASE_URL", "http://127.0.0.1")
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", "10.0.0.0/8,127.0.0.1")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")

    port = _pick_free_port()
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        forwarded_allow_ips=PRODUCTION_FORWARDED_ALLOW_IPS,
        log_level="warning",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    with patch("app.db.init_db"):
        thread.start()
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                with httpx.Client() as client:
                    if client.get(f"http://127.0.0.1:{port}/health", timeout=1).status_code == 200:
                        break
            except (httpx.HTTPError, OSError):
                time.sleep(0.2)
        else:
            server.should_exit = True
            thread.join(timeout=2)
            raise AssertionError("uvicorn did not become ready")

        try:
            store = FakeRateLimitStore()
            with shared_rate_limiter(store), mock_db_connection():
                with httpx.Client() as client:
                    direct = client.get(
                        f"http://127.0.0.1:{port}/admin/login",
                        headers={"X-Forwarded-For": SPOOFED_CLIENT},
                        timeout=5,
                    )
                    assert direct.status_code == 200

                    for spoofed in (SPOOFED_CLIENT, OTHER_CLIENT, "203.0.113.1"):
                        form = client.get(f"http://127.0.0.1:{port}/admin/login", timeout=5)
                        csrf = _extract_csrf(form.text)
                        flow_cookie = form.cookies.get(admin_auth.LOGIN_FLOW_COOKIE_NAME)
                        response = client.post(
                            f"http://127.0.0.1:{port}/admin/login",
                            data={
                                "username": "ghost",
                                "password": "wrong-password",
                                "csrf_token": csrf,
                            },
                            cookies={admin_auth.LOGIN_FLOW_COOKIE_NAME: flow_cookie or ""},
                            headers={
                                "CF-Connecting-IP": REAL_CLIENT,
                                "X-Forwarded-For": f"{spoofed}, {REAL_CLIENT}",
                            },
                            timeout=5,
                        )
                        if response.status_code == 429:
                            break
                        assert response.status_code == 401
                    else:
                        pytest.fail("expected throttle after repeated spoofed-header attempts")
                source_key = admin_auth.build_source_rate_limit_key(REAL_CLIENT)
                assert list(store.rows.keys()) == [source_key]
        finally:
            server.should_exit = True
            thread.join(timeout=5)


def _extract_csrf(html: str) -> str:
    import re

    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match is not None
    return match.group(1)
