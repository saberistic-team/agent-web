"""Tests for trusted-proxy admin login client source resolution (#239)."""

from __future__ import annotations

import logging
import socket
import threading
import time
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import Request

from app import admin_auth
from app.admin_client_source import (
    ClientSourceResolution,
    normalize_client_address,
    resolve_admin_login_client_source,
)
from app.config import Settings, get_settings
from app.main import app

RENDER_PROXY = "10.0.0.5"
CF_EDGE = "104.16.0.10"
CLIENT = "198.51.100.10"
SPOOFED = "203.0.113.1"
UNTRUSTED_PEER = "198.51.100.99"
TRUSTED_CIDRS = "10.0.0.0/8,104.16.0.0/13"


def _request(
    *,
    peer: str,
    headers: list[tuple[bytes, bytes]] | None = None,
) -> Request:
    scope = {
        "type": "http",
        "headers": headers or [],
        "client": (peer, 12345),
        "method": "POST",
        "path": "/admin/login",
    }
    return Request(scope)


def _settings(
    *,
    trust_proxy: bool = False,
    trusted_cidrs: str = TRUSTED_CIDRS,
) -> Settings:
    return Settings(
        database_url="postgresql://test:test@localhost:5432/test",
        stripe_secret_key="",
        stripe_webhook_secret="",
        stripe_publishable_key="",
        resend_api_key="",
        from_email="noreply@example.com",
        notify_email="ops@example.com",
        base_url="http://testserver",
        plausible_domain="",
        plausible_api_key="",
        analytics_environment="test",
        admin_username="operator",
        admin_password_hash="hash",
        admin_session_secret="secret",
        admin_trust_proxy_headers=trust_proxy,
        admin_trusted_proxy_cidrs=tuple(
            part.strip() for part in trusted_cidrs.split(",") if part.strip()
        ),
    )


def _resolve(
    *,
    peer: str,
    trust_proxy: bool = False,
    headers: dict[str, str] | None = None,
) -> ClientSourceResolution:
    header_list = (
        [(key.lower().encode("ascii"), value.encode("ascii")) for key, value in headers.items()]
        if headers
        else []
    )
    request = _request(peer=peer, headers=header_list)
    return resolve_admin_login_client_source(request, _settings(trust_proxy=trust_proxy))


@pytest.fixture
def proxy_trust_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", TRUSTED_CIDRS)


@pytest.mark.unit
def test_direct_spoof_single_value_ignored_without_trust() -> None:
    resolution = _resolve(
        peer=UNTRUSTED_PEER,
        headers={"X-Forwarded-For": SPOOFED},
    )
    assert resolution.source == UNTRUSTED_PEER
    assert resolution.path == "direct_peer"


@pytest.mark.unit
def test_direct_spoof_multi_value_ignored_without_trust() -> None:
    resolution = _resolve(
        peer=UNTRUSTED_PEER,
        headers={"X-Forwarded-For": f"{SPOOFED}, {CLIENT}"},
    )
    assert resolution.source == UNTRUSTED_PEER
    assert resolution.path == "direct_peer"


@pytest.mark.unit
def test_direct_spoof_ignored_when_peer_not_trusted() -> None:
    resolution = _resolve(
        peer=UNTRUSTED_PEER,
        trust_proxy=True,
        headers={"X-Forwarded-For": f"{SPOOFED}, {CLIENT}"},
    )
    assert resolution.source == UNTRUSTED_PEER
    assert resolution.path == "untrusted_peer"


@pytest.mark.unit
def test_cloudflare_append_ignores_attacker_leftmost() -> None:
    resolution = _resolve(
        peer=RENDER_PROXY,
        trust_proxy=True,
        headers={"X-Forwarded-For": f"{SPOOFED}, {CLIENT}"},
    )
    assert resolution.source == CLIENT
    assert resolution.path == "x_forwarded_for"


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client() -> None:
    resolution = _resolve(
        peer=RENDER_PROXY,
        trust_proxy=True,
        headers={
            "X-Forwarded-For": f"{CLIENT}, {CF_EDGE}, {RENDER_PROXY}",
            "CF-Connecting-IP": CLIENT,
        },
    )
    assert resolution.source == CLIENT
    assert resolution.path == "cf_connecting_ip"


@pytest.mark.unit
def test_partial_trust_untrusted_peer_fails_closed() -> None:
    resolution = _resolve(
        peer=UNTRUSTED_PEER,
        trust_proxy=True,
        headers={
            "X-Forwarded-For": f"{CLIENT}, {RENDER_PROXY}",
            "CF-Connecting-IP": CLIENT,
        },
    )
    assert resolution.source == UNTRUSTED_PEER
    assert resolution.path == "untrusted_peer"


@pytest.mark.unit
def test_direct_render_origin_ignores_spoofed_cloudflare_header() -> None:
    resolution = _resolve(
        peer=UNTRUSTED_PEER,
        trust_proxy=True,
        headers={
            "CF-Connecting-IP": SPOOFED,
            "X-Forwarded-For": SPOOFED,
        },
    )
    assert resolution.source == UNTRUSTED_PEER
    assert resolution.path == "untrusted_peer"


@pytest.mark.unit
def test_single_hop_xff_fails_closed_without_corroboration() -> None:
    resolution = _resolve(
        peer=RENDER_PROXY,
        trust_proxy=True,
        headers={"X-Forwarded-For": SPOOFED},
    )
    assert resolution.source == "unknown"
    assert resolution.path == "missing_forwarding"


@pytest.mark.unit
def test_single_hop_xff_accepted_with_matching_cf_header() -> None:
    resolution = _resolve(
        peer=RENDER_PROXY,
        trust_proxy=True,
        headers={
            "X-Forwarded-For": CLIENT,
            "CF-Connecting-IP": CLIENT,
        },
    )
    assert resolution.source == CLIENT
    assert resolution.path == "x_forwarded_for"


@pytest.mark.unit
def test_header_precedence_cf_over_conflicting_xff() -> None:
    resolution = _resolve(
        peer=RENDER_PROXY,
        trust_proxy=True,
        headers={
            "X-Forwarded-For": f"{SPOOFED}, {CF_EDGE}, {RENDER_PROXY}",
            "CF-Connecting-IP": CLIENT,
            "Forwarded": f'for="{SPOOFED}"',
        },
    )
    assert resolution.source == CLIENT
    assert resolution.path == "cf_connecting_ip"


@pytest.mark.unit
def test_forwarded_header_used_when_xff_missing() -> None:
    resolution = _resolve(
        peer=RENDER_PROXY,
        trust_proxy=True,
        headers={"Forwarded": f'for="{CLIENT}", for={RENDER_PROXY}'},
    )
    assert resolution.source == CLIENT
    assert resolution.path == "forwarded"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("203.0.113.1", "203.0.113.1"),
        ("203.0.113.1:8080", "203.0.113.1"),
        ("2001:db8::1", "2001:db8::1"),
        ("[2001:db8::1]:443", "2001:db8::1"),
        ("::ffff:203.0.113.1", "203.0.113.1"),
        ("  203.0.113.1  ", "203.0.113.1"),
        ("", None),
        ("not-an-ip", None),
    ],
)
def test_normalize_client_address(raw: str, expected: str | None) -> None:
    assert normalize_client_address(raw) == expected


@pytest.mark.unit
def test_malformed_forward_chain_fails_closed() -> None:
    resolution = _resolve(
        peer=RENDER_PROXY,
        trust_proxy=True,
        headers={"X-Forwarded-For": "not-an-ip, also-bad"},
    )
    assert resolution.source == "unknown"
    assert resolution.path == "missing_forwarding"


@pytest.mark.unit
def test_overlong_forward_chain_fails_closed() -> None:
    hops = ", ".join(f"10.0.{index}.1" for index in range(40))
    resolution = _resolve(
        peer=RENDER_PROXY,
        trust_proxy=True,
        headers={"X-Forwarded-For": hops},
    )
    assert resolution.source == "unknown"
    assert resolution.path == "missing_forwarding"


@pytest.mark.unit
def test_whitespace_and_empty_elements_are_ignored() -> None:
    resolution = _resolve(
        peer=RENDER_PROXY,
        trust_proxy=True,
        headers={"X-Forwarded-For": f"  {SPOOFED} , , {CLIENT}  , {RENDER_PROXY} "},
    )
    assert resolution.source == CLIENT


@pytest.mark.unit
def test_rotating_spoofed_headers_share_one_limiter_key(
    rate_limit_store: Any,
    proxy_trust_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_admin_auth import FakeRateLimitStore, shared_rate_limiter

    store: FakeRateLimitStore = rate_limit_store
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "3")

    def _admit_with_peer(peer: str, xff: str) -> admin_auth.LoginAdmissionResult:
        request = _request(
            peer=peer,
            headers=[(b"x-forwarded-for", xff.encode("ascii"))],
        )
        return admin_auth.try_admit_login_attempt(
            request,
            get_settings(),
            username="ghost",
        )

    with shared_rate_limiter(store):
        assert _admit_with_peer(RENDER_PROXY, f"{SPOOFED}, {CLIENT}").admitted
        assert _admit_with_peer(RENDER_PROXY, f"203.0.113.2, {CLIENT}").admitted
        assert _admit_with_peer(RENDER_PROXY, f"203.0.113.3, {CLIENT}").admitted
        blocked = _admit_with_peer(RENDER_PROXY, f"203.0.113.4, {CLIENT}")
        assert not blocked.admitted
        assert blocked.throttled

    source_key = admin_auth.build_source_rate_limit_key(CLIENT)
    assert len(store.rows) == 1
    assert source_key in store.rows


@pytest.mark.unit
def test_non_ip_peer_used_for_testclient() -> None:
    resolution = _resolve(peer="testclient")
    assert resolution.source == "testclient"
    assert resolution.path == "direct_peer"


@pytest.mark.unit
def test_telemetry_contains_no_raw_addresses(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="app.admin_client_source")
    _resolve(
        peer=RENDER_PROXY,
        trust_proxy=True,
        headers={
            "X-Forwarded-For": f"{SPOOFED}, {CLIENT}, {CF_EDGE}",
            "CF-Connecting-IP": CLIENT,
        },
    )
    for record in caplog.records:
        message = record.getMessage()
        assert SPOOFED not in message
        assert CLIENT not in message
        assert CF_EDGE not in message
        extra = getattr(record, "client_source_path", None)
        if extra is not None:
            assert SPOOFED not in str(extra)
    assert any(
        getattr(record, "client_source_path", "") == "cf_connecting_ip"
        for record in caplog.records
    )


@pytest.mark.unit
def test_limiter_key_uses_digest_not_raw_source() -> None:
    key = admin_auth.build_source_rate_limit_key(CLIENT)
    assert CLIENT not in key
    assert len(key) == 64


@pytest.fixture
def rate_limit_store() -> Any:
    from tests.test_admin_auth import FakeRateLimitStore

    return FakeRateLimitStore()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.mark.integration
def test_uvicorn_deployment_proxy_settings_match_render_yaml(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Uvicorn runs with the same explicit forwarded-header denial as render.yaml."""
    port = _free_port()
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        forwarded_allow_ips="",
    )
    assert config.forwarded_allow_ips == ""
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    if not server.started:
        pytest.fail("uvicorn server failed to start")
    try:
        with httpx.Client() as http:
            health = http.get(f"http://127.0.0.1:{port}/health")
            assert health.status_code == 200
            assert health.json()["status"] == "ok"

            login = http.get(
                f"http://127.0.0.1:{port}/admin/login",
                headers={
                    "X-Forwarded-For": f"{SPOOFED}, {CLIENT}",
                    "CF-Connecting-IP": CLIENT,
                },
            )
    finally:
        server.should_exit = True
        thread.join(timeout=5)

    # Peer is 127.0.0.1 (untrusted); spoofed forwarding must not bypass the limiter gate.
    assert login.status_code in {200, 303, 503}
    assert SPOOFED not in login.text
