"""Unit tests for trusted admin login client source resolution."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi import Request

from app import admin_auth
from app.admin_client_source import (
    ClientSourceResolutionPath,
    normalize_client_source,
    reset_client_source_telemetry,
    resolve_admin_login_client_source,
)
from app.config import Settings, get_settings

RENDER_TRUSTED_PEER = "10.0.0.1"
RENDER_TRUSTED_SETTINGS = Settings(
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
    admin_session_secret="secret-secret-secret-secret",
    admin_trusted_proxy_cidrs=("10.0.0.0/8",),
)


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


@pytest.fixture(autouse=True)
def _reset_telemetry() -> None:
    reset_client_source_telemetry()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("203.0.113.1", "203.0.113.1"),
        ("203.0.113.1:443", "203.0.113.1"),
        ("2001:db8::1", "2001:db8::1"),
        ("[2001:db8::1]:443", "2001:db8::1"),
        ("::ffff:203.0.113.50", "203.0.113.50"),
        (" 203.0.113.2 ", "203.0.113.2"),
        ("not-an-ip", None),
        ("", None),
    ],
)
def test_normalize_client_source(raw: str, expected: str | None) -> None:
    assert normalize_client_source(raw) == expected


@pytest.mark.unit
def test_direct_spoof_single_and_multi_value_xff_ignored() -> None:
    attacker = "198.51.100.10"
    for header_value in (b"203.0.113.99", b"203.0.113.99, 203.0.113.100"):
        request = _request_with_client(
            attacker,
            headers=[(b"x-forwarded-for", header_value)],
        )
        result = resolve_admin_login_client_source(request, RENDER_TRUSTED_SETTINGS)
        assert result.source == attacker
        assert result.path is ClientSourceResolutionPath.DIRECT_PEER


@pytest.mark.unit
def test_cloudflare_append_preserves_spoofed_leftmost_but_selects_real_client() -> None:
    request = _request_with_client(
        RENDER_TRUSTED_PEER,
        headers=[(b"x-forwarded-for", b"203.0.113.99, 198.51.100.10")],
    )
    result = resolve_admin_login_client_source(request, RENDER_TRUSTED_SETTINGS)
    assert result.source == "198.51.100.10"
    assert result.path is ClientSourceResolutionPath.X_FORWARDED_FOR


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client() -> None:
    request = _request_with_client(
        RENDER_TRUSTED_PEER,
        headers=[(b"x-forwarded-for", b"203.0.113.50, 10.0.0.2")],
    )
    result = resolve_admin_login_client_source(request, RENDER_TRUSTED_SETTINGS)
    assert result.source == "203.0.113.50"
    assert result.path is ClientSourceResolutionPath.X_FORWARDED_FOR


@pytest.mark.unit
def test_partial_trust_untrusted_intermediary_ignores_forwarded_chain() -> None:
    """Untrusted peer cannot inherit a client identity from XFF trusted hops."""
    request = _request_with_client(
        "198.51.100.10",
        headers=[(b"x-forwarded-for", b"203.0.113.50, 10.0.0.1")],
    )
    result = resolve_admin_login_client_source(request, RENDER_TRUSTED_SETTINGS)
    assert result.source == "198.51.100.10"
    assert result.path is ClientSourceResolutionPath.DIRECT_PEER


@pytest.mark.unit
def test_direct_render_origin_ignores_cloudflare_headers() -> None:
    request = _request_with_client(
        "203.0.113.77",
        headers=[
            (b"cf-connecting-ip", b"203.0.113.99"),
            (b"x-forwarded-for", b"203.0.113.99"),
            (b"forwarded", b'for=203.0.113.99;proto=https'),
        ],
    )
    result = resolve_admin_login_client_source(request, RENDER_TRUSTED_SETTINGS)
    assert result.source == "203.0.113.77"
    assert result.path is ClientSourceResolutionPath.DIRECT_PEER


@pytest.mark.unit
def test_header_precedence_cf_connecting_ip_over_conflicting_xff_and_forwarded() -> None:
    request = _request_with_client(
        RENDER_TRUSTED_PEER,
        headers=[
            (b"cf-connecting-ip", b"203.0.113.50"),
            (b"x-forwarded-for", b"203.0.113.99, 198.51.100.10"),
            (b"forwarded", b'for=203.0.113.88;proto=https'),
        ],
    )
    result = resolve_admin_login_client_source(request, RENDER_TRUSTED_SETTINGS)
    assert result.source == "203.0.113.50"
    assert result.path is ClientSourceResolutionPath.CF_CONNECTING_IP


@pytest.mark.unit
def test_forwarded_header_used_when_trusted_and_no_cf_or_xff() -> None:
    request = _request_with_client(
        RENDER_TRUSTED_PEER,
        headers=[(b"forwarded", b'for="[2001:db8::5]";proto=https')],
    )
    result = resolve_admin_login_client_source(request, RENDER_TRUSTED_SETTINGS)
    assert result.source == "2001:db8::5"
    assert result.path is ClientSourceResolutionPath.FORWARDED


@pytest.mark.unit
def test_excessive_chain_length_fails_closed() -> None:
    chain = ", ".join(f"10.0.0.{index}" for index in range(1, 13))
    request = _request_with_client(
        RENDER_TRUSTED_PEER,
        headers=[(b"x-forwarded-for", chain.encode("ascii"))],
    )
    result = resolve_admin_login_client_source(request, RENDER_TRUSTED_SETTINGS)
    assert result.source == "unknown"
    assert result.path is ClientSourceResolutionPath.INVALID_FORWARDING


@pytest.mark.unit
def test_empty_xff_elements_and_whitespace_are_skipped() -> None:
    request = _request_with_client(
        RENDER_TRUSTED_PEER,
        headers=[(b"x-forwarded-for", b" 203.0.113.50 , , 10.0.0.2 ")],
    )
    result = resolve_admin_login_client_source(request, RENDER_TRUSTED_SETTINGS)
    assert result.source == "203.0.113.50"


@pytest.mark.unit
def test_missing_peer_returns_unknown() -> None:
    scope = {
        "type": "http",
        "headers": [],
        "client": None,
        "method": "POST",
        "path": "/admin/login",
    }
    request = Request(scope)
    result = resolve_admin_login_client_source(request, RENDER_TRUSTED_SETTINGS)
    assert result.source == "unknown"
    assert result.path is ClientSourceResolutionPath.UNKNOWN


@pytest.mark.unit
def test_legacy_admin_trust_proxy_headers_enables_default_cidrs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    settings = get_settings()
    request = _request_with_client(
        RENDER_TRUSTED_PEER,
        headers=[(b"x-forwarded-for", b"203.0.113.50, 10.0.0.2")],
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.source == "203.0.113.50"


@pytest.mark.unit
def test_client_ip_emits_resolution_path_without_raw_addresses(
    caplog: pytest.LogCaptureFixture,
) -> None:
    request = _request_with_client(
        RENDER_TRUSTED_PEER,
        headers=[(b"cf-connecting-ip", b"203.0.113.50")],
    )
    with caplog.at_level(logging.INFO):
        source = admin_auth.client_ip(request, RENDER_TRUSTED_SETTINGS)
    assert source == "203.0.113.50"
    assert "203.0.113.50" not in caplog.text
    resolution_logs = [
        record
        for record in caplog.records
        if record.getMessage() == "Admin login client source resolved"
    ]
    assert resolution_logs
    assert resolution_logs[0].source_resolution_path == "cf_connecting_ip"  # type: ignore[attr-defined]


@pytest.mark.unit
def test_invalid_forwarding_emits_sampled_telemetry(
    caplog: pytest.LogCaptureFixture,
) -> None:
    request = _request_with_client(
        RENDER_TRUSTED_PEER,
        headers=[(b"x-forwarded-for", b"not-an-ip, 10.0.0.2")],
    )
    with caplog.at_level(logging.WARNING):
        resolve_admin_login_client_source(request, RENDER_TRUSTED_SETTINGS)
        resolve_admin_login_client_source(request, RENDER_TRUSTED_SETTINGS)
    warning_records = [
        record
        for record in caplog.records
        if record.getMessage()
        == "Admin login source resolution rejected forwarding headers"
    ]
    assert len(warning_records) == 1
    assert warning_records[0].source_resolution_path == "invalid_forwarding"  # type: ignore[attr-defined]
    assert "not-an-ip" not in caplog.text


@pytest.mark.unit
def test_limiter_keys_never_embed_raw_source_material() -> None:
    source = "203.0.113.50"
    key = admin_auth.build_source_rate_limit_key(source)
    assert source not in key
    assert len(key) == 64
