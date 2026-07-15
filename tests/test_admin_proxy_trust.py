"""Trusted-proxy admin login source resolution (#239)."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import pytest

from app import admin_auth
from app.config import get_settings
from app.main import app
from app.proxy_trust import (
    PRODUCTION_FORWARDED_ALLOW_IPS,
    SourceResolutionPath,
    normalize_client_address,
    reset_invalid_forwarding_telemetry,
    resolve_admin_login_client_source,
)
from tests.test_admin_auth import (
    FakeRateLimitStore,
    TEST_HASH,
    TEST_SECRET,
    TEST_USERNAME,
    _extract_csrf_token,
    _request_with_client,
    mock_db_connection,
    shared_rate_limiter,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
RENDER_LB = "10.0.0.1"
CLOUDFLARE_EDGE = "172.18.0.1"
REAL_CLIENT = "203.0.113.77"
SPOOFED_CLIENT = "198.51.100.99"
DIRECT_PEER = "198.51.100.10"
TEST_TRUSTED_CIDRS = ("10.0.0.0/8", "172.16.0.0/12")


def _settings_with_proxy_trust(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", ",".join(TEST_TRUSTED_CIDRS))
    return get_settings()


def _resolve(
    *,
    peer: str,
    headers: dict[str, str] | None = None,
    trust: bool = True,
    trusted_cidrs: tuple[str, ...] = TEST_TRUSTED_CIDRS,
) -> str:
    request = _request_with_client(peer)
    if headers:
        for name, value in headers.items():
            request.headers.__dict__["_list"].append((name.lower().encode(), value.encode()))
    settings = get_settings()
    return resolve_admin_login_client_source(
        socket_peer=peer,
        headers=request.headers,
        trust_proxy_headers=trust,
        trusted_proxy_cidrs=trusted_cidrs,
    ).source


@pytest.fixture
def rate_limit_store() -> FakeRateLimitStore:
    return FakeRateLimitStore()


@pytest.fixture(autouse=True)
def proxy_trust_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    monkeypatch.delenv("ADMIN_FORWARDED_ALLOW_IPS", raising=False)
    admin_auth.reset_login_rate_limiter()
    reset_invalid_forwarding_telemetry()


@pytest.mark.unit
def test_direct_spoof_single_and_multi_value_xff_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings_with_proxy_trust(monkeypatch)
    for header_value in (
        SPOOFED_CLIENT,
        f"{SPOOFED_CLIENT}, {REAL_CLIENT}",
        f"{SPOOFED_CLIENT}, {REAL_CLIENT}, {RENDER_LB}",
    ):
        assert (
            _resolve(peer=DIRECT_PEER, headers={"X-Forwarded-For": header_value})
            == DIRECT_PEER
        )


@pytest.mark.unit
def test_cloudflare_append_ignores_attacker_leftmost_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings_with_proxy_trust(monkeypatch)
    chain = f"{SPOOFED_CLIENT}, {REAL_CLIENT}"
    assert _resolve(peer=RENDER_LB, headers={"X-Forwarded-For": chain}) == REAL_CLIENT


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings_with_proxy_trust(monkeypatch)
    chain = f"{REAL_CLIENT}, {CLOUDFLARE_EDGE}, {RENDER_LB}"
    assert _resolve(peer=RENDER_LB, headers={"X-Forwarded-For": chain}) == REAL_CLIENT


@pytest.mark.unit
def test_partial_trust_fails_closed_behind_untrusted_intermediary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings_with_proxy_trust(monkeypatch)
    untrusted_proxy = "203.0.113.1"
    chain = f"{REAL_CLIENT}, {RENDER_LB}"
    assert _resolve(peer=untrusted_proxy, headers={"X-Forwarded-For": chain}) == untrusted_proxy


@pytest.mark.unit
def test_direct_render_origin_ignores_cloudflare_vendor_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings_with_proxy_trust(monkeypatch)
    assert (
        _resolve(
            peer=DIRECT_PEER,
            headers={
                "CF-Connecting-IP": REAL_CLIENT,
                "X-Forwarded-For": REAL_CLIENT,
            },
        )
        == DIRECT_PEER
    )


@pytest.mark.unit
def test_header_precedence_cf_connecting_ip_when_trusted_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings_with_proxy_trust(monkeypatch)
    resolution = resolve_admin_login_client_source(
        socket_peer=RENDER_LB,
        headers={
            "CF-Connecting-IP": REAL_CLIENT,
            "X-Forwarded-For": f"{REAL_CLIENT}, {CLOUDFLARE_EDGE}",
        },
        trust_proxy_headers=True,
        trusted_proxy_cidrs=TEST_TRUSTED_CIDRS,
    )
    assert resolution.source == REAL_CLIENT
    assert resolution.path == SourceResolutionPath.CF_CONNECTING_IP


@pytest.mark.unit
def test_header_conflict_prefers_x_forwarded_for_over_cf_connecting_ip(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _settings_with_proxy_trust(monkeypatch)
    with caplog.at_level(logging.WARNING):
        resolution = resolve_admin_login_client_source(
            socket_peer=RENDER_LB,
            headers={
                "CF-Connecting-IP": SPOOFED_CLIENT,
                "X-Forwarded-For": f"{REAL_CLIENT}, {CLOUDFLARE_EDGE}",
            },
            trust_proxy_headers=True,
            trusted_proxy_cidrs=TEST_TRUSTED_CIDRS,
        )
    assert resolution.source == REAL_CLIENT
    assert resolution.path == SourceResolutionPath.X_FORWARDED_FOR
    assert any(
        getattr(record, "reason", None) == "header_family_conflict"
        for record in caplog.records
    )


@pytest.mark.unit
def test_forwarded_header_right_to_left_precedence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings_with_proxy_trust(monkeypatch)
    forwarded = (
        f'for="{RENDER_LB}";proto=https, '
        f'for="{CLOUDFLARE_EDGE}";proto=https, '
        f'for="{REAL_CLIENT}";proto=https'
    )
    resolution = resolve_admin_login_client_source(
        socket_peer=RENDER_LB,
        headers={"Forwarded": forwarded},
        trust_proxy_headers=True,
        trusted_proxy_cidrs=TEST_TRUSTED_CIDRS,
    )
    assert resolution.source == REAL_CLIENT
    assert resolution.path == SourceResolutionPath.FORWARDED


@pytest.mark.unit
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
        ("x" * 300, None),
    ],
)
def test_address_normalization_formats(raw: str, expected: str | None) -> None:
    assert normalize_client_address(raw) == expected


@pytest.mark.unit
def test_excessive_forwarding_chain_is_conservative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings_with_proxy_trust(monkeypatch)
    long_chain = ", ".join(f"203.0.113.{index}" for index in range(40))
    resolution = resolve_admin_login_client_source(
        socket_peer=RENDER_LB,
        headers={"X-Forwarded-For": long_chain},
        trust_proxy_headers=True,
        trusted_proxy_cidrs=TEST_TRUSTED_CIDRS,
    )
    assert resolution.path == SourceResolutionPath.INVALID
    assert resolution.source == RENDER_LB


@pytest.mark.unit
@pytest.mark.integration
def test_rotating_spoofed_headers_does_not_create_new_limiter_rows(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "3")
    _settings_with_proxy_trust(monkeypatch)

    def trusted_login(headers: dict[str, str]) -> Any:
        peer_client = _trusted_test_client(app, peer=RENDER_LB)
        with mock_db_connection():
            form = peer_client.get("/admin/login")
            csrf_token, cookies = _parse_login_form(form)
            return peer_client.post(
                "/admin/login",
                data={
                    "username": "ghost",
                    "password": "wrong",
                    "csrf_token": csrf_token,
                },
                cookies=cookies,
                headers=headers,
            )

    with shared_rate_limiter(rate_limit_store):
        for index in range(3):
            response = trusted_login(
                {"X-Forwarded-For": f"203.0.113.{index}, {REAL_CLIENT}"}
            )
            assert response.status_code == 401

        blocked = trusted_login({"X-Forwarded-For": f"203.0.113.99, {REAL_CLIENT}"})
        assert blocked.status_code == 429
        assert len(rate_limit_store.rows) == 1


class _PeerApp:
    """ASGI wrapper that injects a deterministic socket peer."""

    def __init__(self, app: Any, peer: str) -> None:
        self._app = app
        self._peer = peer

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            scope = dict(scope)
            scope["client"] = (self._peer, 12345)
        await self._app(scope, receive, send)


def _trusted_test_client(asgi_app: Any, *, peer: str) -> Any:
    from fastapi.testclient import TestClient

    return TestClient(_PeerApp(asgi_app, peer), follow_redirects=False)


def _parse_login_form(response: Any) -> tuple[str, dict[str, str]]:
    csrf_token = _extract_csrf_token(response.text)
    flow_cookie = response.cookies.get(admin_auth.LOGIN_FLOW_COOKIE_NAME)
    assert flow_cookie
    return csrf_token, {admin_auth.LOGIN_FLOW_COOKIE_NAME: flow_cookie}


@pytest.mark.unit
def test_render_yaml_proxy_settings_are_consistent() -> None:
    render_yaml = (REPO_ROOT / "render.yaml").read_text()
    assert "--forwarded-allow-ips" in render_yaml
    assert PRODUCTION_FORWARDED_ALLOW_IPS in render_yaml
    assert "ADMIN_TRUST_PROXY_HEADERS" in render_yaml
    assert "ADMIN_FORWARDED_ALLOW_IPS" in render_yaml
    assert 'value: "true"' in render_yaml or "value: 'true'" in render_yaml
    assert PRODUCTION_FORWARDED_ALLOW_IPS in render_yaml


@pytest.mark.unit
def test_health_reports_proxy_trust_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_FORWARDED_ALLOW_IPS", PRODUCTION_FORWARDED_ALLOW_IPS)
    from fastapi.testclient import TestClient

    response = TestClient(app).get("/health")
    payload = response.json()
    assert payload["admin_proxy_trust"] == {
        "enabled": True,
        "forwarded_allow_ips": PRODUCTION_FORWARDED_ALLOW_IPS,
        "trusted_proxy_cidrs_configured": False,
    }


@pytest.mark.unit
def test_privacy_no_raw_addresses_in_logs_or_limiter_rows(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _settings_with_proxy_trust(monkeypatch)
    request = _request_with_client(RENDER_LB)
    request.headers.__dict__["_list"].append(
        (b"x-forwarded-for", f"{REAL_CLIENT}, {CLOUDFLARE_EDGE}".encode())
    )
    settings = get_settings()

    with caplog.at_level(logging.INFO):
        source = admin_auth.client_ip(request, settings)

    assert source == REAL_CLIENT
    assert REAL_CLIENT not in caplog.text
    assert CLOUDFLARE_EDGE not in caplog.text
    assert any(
        record.message == "Admin login source resolved"
        for record in caplog.records
    )

    limiter_key = admin_auth.build_source_rate_limit_key(source)
    assert REAL_CLIENT not in limiter_key
    assert re.fullmatch(r"[0-9a-f]{64}", limiter_key)


@pytest.mark.unit
@pytest.mark.integration
def test_uvicorn_proxy_middleware_matches_deployment_forwarded_allow_ips(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise uvicorn ProxyHeadersMiddleware with the render.yaml CIDR boundary."""
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    _settings_with_proxy_trust(monkeypatch)
    wrapped_app = ProxyHeadersMiddleware(app, trusted_hosts=PRODUCTION_FORWARDED_ALLOW_IPS)
    peer_client = _trusted_test_client(wrapped_app, peer=RENDER_LB)

    with shared_rate_limiter(rate_limit_store):
        for index in range(3):
            with mock_db_connection():
                form = peer_client.get("/admin/login")
                csrf_token, cookies = _parse_login_form(form)
                response = peer_client.post(
                    "/admin/login",
                    data={
                        "username": "ghost",
                        "password": "wrong",
                        "csrf_token": csrf_token,
                    },
                    cookies=cookies,
                    headers={"X-Forwarded-For": f"203.0.113.{index}, {REAL_CLIENT}"},
                )
            if index < 2:
                assert response.status_code == 401
            else:
                assert response.status_code == 429
        assert len(rate_limit_store.rows) == 1


@pytest.mark.unit
def test_render_start_command_documents_forwarded_allow_ips() -> None:
    render_yaml = (REPO_ROOT / "render.yaml").read_text()
    assert "uvicorn app.main:app" in render_yaml
    assert PRODUCTION_FORWARDED_ALLOW_IPS in render_yaml
