"""Tests for trusted-proxy admin login client source resolution (#239)."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Generator
import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from app import admin_auth
from app.admin_client_source import (
    DEFAULT_TRUSTED_PROXY_CIDRS,
    SourceResolutionPath,
    client_ip,
    normalize_client_address,
    parse_cidr_list,
    parse_forwarded_header_chain,
    parse_x_forwarded_for_chain,
    resolve_admin_login_client_source,
    resolve_client_from_forwarded_chain,
)
from app.config import get_settings
from app.main import app
from tests.test_admin_auth import (
    FakeRateLimitStore,
    TEST_HASH,
    TEST_PASSWORD,
    TEST_USERNAME,
    _extract_csrf_token,
    shared_rate_limiter,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
RENDER_TRUSTED_CIDRS = (
    "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.1/32,::1/128,fc00::/7"
)
RENDER_START_COMMAND = (
    "uvicorn app.main:app --host 0.0.0.0 --port $PORT --proxy-headers "
    "--forwarded-allow-ips 10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,"
    "127.0.0.1,::1,fc00::/7"
)
TEST_CLOUDFLARE_EDGE = "103.21.244.4"
TEST_RENDER_PEER = "10.0.0.55"
TEST_CLIENT_IPV4 = "203.0.113.44"
TEST_CLIENT_IPV6 = "2001:db8::9"


class _ClientHostMiddleware:
    def __init__(self, inner_app, host: str) -> None:
        self._inner_app = inner_app
        self._host = host

    async def __call__(self, scope, receive, send):  # noqa: ANN001
        if scope["type"] == "http":
            scope = {**scope, "client": (self._host, 54321)}
        await self._inner_app(scope, receive, send)


@contextmanager
def peer_test_client(host: str) -> Generator[TestClient, None, None]:
    wrapped = _ClientHostMiddleware(app, host)
    yield TestClient(wrapped, follow_redirects=False)


def _request_with_client(host: str, headers: list[tuple[bytes, bytes]] | None = None) -> Request:
    scope = {
        "type": "http",
        "headers": headers or [],
        "client": (host, 12345),
        "method": "POST",
        "path": "/admin/login",
    }
    return Request(scope)


def _trusted_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "test-session-secret-32chars-minimum")
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", RENDER_TRUSTED_CIDRS)
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)


@pytest.fixture
def rate_limit_store() -> FakeRateLimitStore:
    return FakeRateLimitStore()


@pytest.fixture(autouse=True)
def admin_client_source_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _trusted_settings(monkeypatch)
    admin_auth.reset_login_rate_limiter()


@pytest.mark.unit
def test_parse_cidr_list_ignores_empty_tokens() -> None:
    assert parse_cidr_list("10.0.0.0/8, ,172.16.0.0/12") == (
        "10.0.0.0/8",
        "172.16.0.0/12",
    )


@pytest.mark.unit
def test_normalize_client_address_formats() -> None:
    assert normalize_client_address("203.0.113.1") == "203.0.113.1"
    assert normalize_client_address("203.0.113.1:443") == "203.0.113.1"
    assert normalize_client_address("2001:db8::1") == "2001:db8::1"
    assert normalize_client_address("::ffff:203.0.113.9") == "203.0.113.9"
    assert normalize_client_address("[2001:db8::9]:443") == "2001:db8::9"
    assert normalize_client_address(' "203.0.113.2" ') == "203.0.113.2"
    assert normalize_client_address("") is None
    assert normalize_client_address("not-an-ip") is None
    assert normalize_client_address("x" * 129) is None


@pytest.mark.unit
def test_direct_spoof_ignores_forwarded_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    settings = get_settings()
    direct_peer = "198.51.100.10"

    single = _request_with_client(
        direct_peer,
        headers=[(b"x-forwarded-for", b"203.0.113.99")],
    )
    multi = _request_with_client(
        direct_peer,
        headers=[(b"x-forwarded-for", b"203.0.113.1, 203.0.113.2, 10.0.0.1")],
    )
    assert client_ip(single, settings) == direct_peer
    assert client_ip(multi, settings) == direct_peer
    assert (
        resolve_admin_login_client_source(single, settings).path
        is SourceResolutionPath.DIRECT_PEER
    )


@pytest.mark.unit
def test_cloudflare_append_selects_connecting_client_not_leftmost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    request = _request_with_client(
        TEST_RENDER_PEER,
        headers=[
            (
                b"x-forwarded-for",
                f"203.0.113.99, {TEST_CLIENT_IPV4}, {TEST_CLOUDFLARE_EDGE}".encode(),
            )
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == TEST_CLIENT_IPV4
    assert resolution.path is SourceResolutionPath.FORWARDED_CHAIN


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client() -> None:
    settings = get_settings()
    request = _request_with_client(
        TEST_RENDER_PEER,
        headers=[
            (
                b"x-forwarded-for",
                f"{TEST_CLIENT_IPV4}, {TEST_CLOUDFLARE_EDGE}, {TEST_RENDER_PEER}".encode(),
            )
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == TEST_CLIENT_IPV4


@pytest.mark.unit
def test_partial_trust_fails_closed_to_peer() -> None:
    settings = get_settings()
    request = _request_with_client(
        "203.0.113.5",
        headers=[
            (
                b"x-forwarded-for",
                f"{TEST_CLIENT_IPV4}, {TEST_RENDER_PEER}".encode(),
            )
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.5"
    assert resolution.path is SourceResolutionPath.DIRECT_PEER


@pytest.mark.unit
def test_direct_render_origin_ignores_cf_connecting_ip() -> None:
    settings = get_settings()
    request = _request_with_client(
        TEST_RENDER_PEER,
        headers=[
            (b"cf-connecting-ip", b"203.0.113.77"),
            (b"x-forwarded-for", b"203.0.113.88"),
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.88"
    assert resolution.path is SourceResolutionPath.FORWARDED_CHAIN


@pytest.mark.unit
def test_cf_connecting_ip_used_when_cloudflare_hop_present() -> None:
    settings = get_settings()
    request = _request_with_client(
        TEST_RENDER_PEER,
        headers=[
            (b"cf-connecting-ip", TEST_CLIENT_IPV4.encode()),
            (
                b"x-forwarded-for",
                f"{TEST_CLOUDFLARE_EDGE}, {TEST_RENDER_PEER}".encode(),
            ),
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == TEST_CLIENT_IPV4
    assert resolution.path is SourceResolutionPath.CF_CONNECTING_IP


@pytest.mark.unit
def test_header_precedence_prefers_x_forwarded_for_over_conflicts() -> None:
    settings = get_settings()
    request = _request_with_client(
        TEST_RENDER_PEER,
        headers=[
            (b"x-forwarded-for", f"{TEST_CLIENT_IPV4}, {TEST_RENDER_PEER}".encode()),
            (b"forwarded", b'for="203.0.113.55"'),
            (b"cf-connecting-ip", b"203.0.113.66"),
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == TEST_CLIENT_IPV4
    assert resolution.path is SourceResolutionPath.FORWARDED_CHAIN


@pytest.mark.unit
def test_forwarded_rfc_header_used_when_xff_missing() -> None:
    settings = get_settings()
    request = _request_with_client(
        TEST_RENDER_PEER,
        headers=[(b"forwarded", f'for="{TEST_CLIENT_IPV4}";proto=https'.encode())],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == TEST_CLIENT_IPV4
    assert resolution.path is SourceResolutionPath.FORWARDED_RFC


@pytest.mark.unit
@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("", None),
        (" , ", None),
        (",203.0.113.1", None),
        ("203.0.113.1," + ",10.0.0.1" * 40, None),
    ],
)
def test_malformed_xff_chains(header: str, expected: list[str] | None) -> None:
    assert parse_x_forwarded_for_chain(header) == expected


@pytest.mark.unit
def test_ipv6_and_mapped_resolution_in_chain() -> None:
    settings = get_settings()
    request = _request_with_client(
        TEST_RENDER_PEER,
        headers=[
            (
                b"x-forwarded-for",
                f"{TEST_CLIENT_IPV6}, ::ffff:{TEST_RENDER_PEER}".encode(),
            )
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == TEST_CLIENT_IPV6


@pytest.mark.unit
def test_overlong_forwarded_header_fails_closed_to_peer() -> None:
    settings = get_settings()
    long_chain = ",".join(["10.0.0.1"] * 40)
    request = _request_with_client(
        TEST_RENDER_PEER,
        headers=[(b"x-forwarded-for", long_chain.encode())],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == TEST_RENDER_PEER
    assert resolution.path is SourceResolutionPath.DIRECT_PEER


@pytest.mark.unit
@pytest.mark.integration
def test_rotating_spoofed_headers_do_not_create_new_limiter_rows(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "3")
    from tests.test_admin_auth import mock_db_connection

    with peer_test_client(TEST_RENDER_PEER) as peer_client:
        with shared_rate_limiter(rate_limit_store):
            with mock_db_connection():
                for index in range(5):
                    csrf_token, cookies = _fetch_login_form_for_client(peer_client)
                    response = peer_client.post(
                        "/admin/login",
                        data={
                            "username": f"rotator-{index}",
                            "password": "wrong",
                            "csrf_token": csrf_token,
                        },
                        cookies=cookies,
                        headers={
                            "X-Forwarded-For": f"203.0.113.{index}, {TEST_CLIENT_IPV4}",
                        },
                    )
                    if index < 3:
                        assert response.status_code == 401
                    else:
                        assert response.status_code == 429

    source_key = admin_auth.build_source_rate_limit_key(TEST_CLIENT_IPV4)
    assert len(rate_limit_store.rows) == 1
    assert source_key in rate_limit_store.rows


def _fetch_login_form_for_client(test_client: TestClient) -> tuple[str, dict[str, str]]:
    from tests.test_admin_auth import _parse_login_form, mock_db_connection

    with mock_db_connection():
        response = test_client.get("/admin/login")
    return _parse_login_form(response)


@pytest.mark.unit
def test_render_yaml_proxy_settings_are_consistent() -> None:
    render_yaml = (REPO_ROOT / "render.yaml").read_text(encoding="utf-8")
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in render_yaml
    assert RENDER_TRUSTED_CIDRS in render_yaml
    assert "--proxy-headers" in render_yaml
    assert "--forwarded-allow-ips" in render_yaml
    for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"):
        assert cidr in render_yaml


@pytest.mark.unit
def test_default_trusted_proxy_cidrs_match_render_start_command() -> None:
    for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"):
        assert cidr in RENDER_START_COMMAND
        assert cidr in DEFAULT_TRUSTED_PROXY_CIDRS


@pytest.mark.unit
def test_telemetry_and_limiter_state_exclude_raw_forwarding_data(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    from tests.test_admin_auth import mock_db_connection

    with peer_test_client(TEST_RENDER_PEER) as peer_client:
        with shared_rate_limiter(rate_limit_store):
            with mock_db_connection():
                for _ in range(2):
                    csrf_token, cookies = _fetch_login_form_for_client(peer_client)
                    peer_client.post(
                        "/admin/login",
                        data={
                            "username": "ghost",
                            "password": "wrong",
                            "csrf_token": csrf_token,
                        },
                        cookies=cookies,
                        headers={
                            "X-Forwarded-For": f"203.0.113.9, {TEST_CLIENT_IPV4}",
                            "Forwarded": 'for="203.0.113.8"',
                            "CF-Connecting-IP": "203.0.113.7",
                        },
                    )

    for limiter_key, row in rate_limit_store.rows.items():
        row_blob = repr(row).lower()
        assert "x-forwarded-for" not in row_blob
        assert "203.0.113" not in limiter_key
        assert "203.0.113" not in row_blob
        assert len(limiter_key) == 64

    for record in caplog.records:
        message = record.getMessage().lower()
        assert "x-forwarded-for" not in message
        assert "cf-connecting-ip" not in message
        assert "203.0.113" not in message


@pytest.mark.unit
def test_untrusted_forwarding_attempt_emits_sampled_path_telemetry(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    import app.admin_client_source as client_source

    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    monkeypatch.setattr(client_source, "_last_untrusted_telemetry_at", 0.0)
    settings = get_settings()
    request = _request_with_client(
        "203.0.113.5",
        headers=[(b"x-forwarded-for", b"203.0.113.9")],
    )
    with caplog.at_level(logging.INFO, logger="app.admin_client_source"):
        resolve_admin_login_client_source(request, settings)
    assert any(
        getattr(record, "resolution_path", None) == SourceResolutionPath.UNTRUSTED_PEER.value
        for record in caplog.records
    )


@pytest.mark.integration
def test_uvicorn_proxy_headers_match_deployment_start_command(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise Uvicorn ProxyHeadersMiddleware with deployment allow-ips."""
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", RENDER_TRUSTED_CIDRS)
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")

    class _TrustedPeerMiddleware:
        def __init__(self, inner_app) -> None:
            self._inner_app = ProxyHeadersMiddleware(
                inner_app,
                trusted_hosts=(
                    "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,"
                    "127.0.0.1,::1,fc00::/7"
                ),
            )

        async def __call__(self, scope, receive, send):  # noqa: ANN001
            if scope["type"] == "http":
                scope = {**scope, "client": (TEST_RENDER_PEER, 54321)}
            await self._inner_app(scope, receive, send)

    proxy_client = TestClient(_TrustedPeerMiddleware(app), follow_redirects=False)
    with shared_rate_limiter(rate_limit_store):
        from tests.test_admin_auth import mock_db_connection

        with mock_db_connection():
            login_page = proxy_client.get("/admin/login")
            csrf_token = _extract_csrf_token(login_page.text)
            cookies = dict(login_page.cookies)
            headers = {
                "X-Forwarded-For": f"203.0.113.99, {TEST_CLIENT_IPV4}",
            }
            for expected_status in (401, 401, 429):
                response = proxy_client.post(
                    "/admin/login",
                    data={
                        "username": "ghost",
                        "password": "wrong",
                        "csrf_token": csrf_token,
                    },
                    cookies=cookies,
                    headers=headers,
                )
                assert response.status_code == expected_status
                if response.status_code == 401:
                    set_cookie = response.headers.get_list("set-cookie")
                    flow_cookies = [
                        value
                        for value in set_cookie
                        if value.startswith(f"{admin_auth.LOGIN_FLOW_COOKIE_NAME}=")
                    ]
                    assert flow_cookies
                    cookies = {
                        admin_auth.LOGIN_FLOW_COOKIE_NAME: flow_cookies[-1]
                        .split("=", 1)[1]
                        .split(";", 1)[0]
                    }
                    csrf_token = _extract_csrf_token(response.text)

    source_key = admin_auth.build_source_rate_limit_key(TEST_CLIENT_IPV4)
    assert len(rate_limit_store.rows) == 1
    assert source_key in rate_limit_store.rows


@pytest.mark.unit
def test_legacy_admin_trust_proxy_headers_enables_default_cidrs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    settings = get_settings()
    request = _request_with_client(
        TEST_RENDER_PEER,
        headers=[(b"x-forwarded-for", f"{TEST_CLIENT_IPV4}, {TEST_RENDER_PEER}".encode())],
    )
    assert client_ip(request, settings) == TEST_CLIENT_IPV4


@pytest.mark.unit
def test_parse_forwarded_header_chain_extracts_for_values() -> None:
    assert parse_forwarded_header_chain('for="203.0.113.1";proto=https') == [
        "203.0.113.1"
    ]
    assert parse_forwarded_header_chain("for=203.0.113.2") == ["203.0.113.2"]


@pytest.mark.unit
def test_resolve_client_from_forwarded_chain_skips_trusted_hops() -> None:
    from app.admin_client_source import _forwarded_hop_networks

    settings = get_settings()
    networks = _forwarded_hop_networks(settings)
    chain = parse_x_forwarded_for_chain(f"{TEST_CLIENT_IPV4}, {TEST_RENDER_PEER}")
    assert chain is not None
    assert (
        resolve_client_from_forwarded_chain(chain, trusted_networks=networks)
        == TEST_CLIENT_IPV4
    )
