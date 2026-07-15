"""Tests for trusted-proxy admin login client source resolution (#239)."""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

import httpx
import pytest
import yaml
from argon2 import PasswordHasher
from fastapi import Request
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app import admin_auth
from app.config import get_settings
from app.main import app
from app.proxy_trust import (
    ClientSourceResolution,
    SourceResolutionPath,
    normalize_client_address,
    resolve_admin_login_client_source,
)
from tests.test_admin_auth import (
    FakeRateLimitStore,
    TEST_USERNAME,
    _login,
    mock_db_connection,
    shared_rate_limiter,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
RENDER_YAML = REPO_ROOT / "render.yaml"
TRUSTED_PROXIES = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.1"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "test-session-secret-32chars-minimum"


@pytest.fixture(autouse=True)
def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "5")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_WINDOW_SECONDS", "900")
    monkeypatch.setenv("ADMIN_LOGIN_LOCKOUT_SECONDS", "900")
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    monkeypatch.delenv("UVICORN_FORWARDED_ALLOW_IPS", raising=False)
    admin_auth.reset_login_rate_limiter()


def _request_with_client(host: str, headers: dict[str, str] | None = None) -> Request:
    header_list = [
        (name.lower().encode("latin-1"), value.encode("latin-1"))
        for name, value in (headers or {}).items()
    ]
    scope = {
        "type": "http",
        "headers": header_list,
        "client": (host, 12345),
        "method": "POST",
        "path": "/admin/login",
    }
    return Request(scope)


@pytest.fixture
def trusted_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", TRUSTED_PROXIES)
    monkeypatch.setenv("UVICORN_FORWARDED_ALLOW_IPS", TRUSTED_PROXIES)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("203.0.113.1", "203.0.113.1"),
        ("203.0.113.1:443", "203.0.113.1"),
        ("[2001:db8::1]", "2001:db8::1"),
        ("[2001:db8::1]:443", "2001:db8::1"),
        ("::ffff:203.0.113.9", "203.0.113.9"),
        ("  203.0.113.2  ", "203.0.113.2"),
        ("", None),
        ("not-an-ip", None),
        ("x" * 300, None),
    ],
)
def test_normalize_client_address(raw: str, expected: str | None) -> None:
    assert normalize_client_address(raw) == expected


@pytest.mark.unit
def test_direct_spoof_ignores_forwarded_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    settings = get_settings()
    request = _request_with_client(
        "198.51.100.10",
        {"X-Forwarded-For": "203.0.113.99"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution == ClientSourceResolution(
        "198.51.100.10",
        SourceResolutionPath.DIRECT_PEER,
    )


@pytest.mark.unit
def test_direct_spoof_ignores_multivalue_forwarded_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    settings = get_settings()
    request = _request_with_client(
        "198.51.100.10",
        {"X-Forwarded-For": "203.0.113.1, 203.0.113.2, 203.0.113.3"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "198.51.100.10"
    assert resolution.path is SourceResolutionPath.DIRECT_PEER


@pytest.mark.unit
def test_cloudflare_append_selects_appended_client_not_leftmost(
    trusted_proxy_env: None,
) -> None:
    settings = get_settings()
    request = _request_with_client(
        "10.0.0.5",
        {"X-Forwarded-For": "203.0.113.55, 203.0.113.77"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution == ClientSourceResolution(
        "203.0.113.77",
        SourceResolutionPath.TRUSTED_FORWARDED,
    )


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client(trusted_proxy_env: None) -> None:
    settings = get_settings()
    request = _request_with_client(
        "10.0.0.5",
        {"X-Forwarded-For": "203.0.113.50, 10.0.0.1"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution == ClientSourceResolution(
        "203.0.113.50",
        SourceResolutionPath.TRUSTED_FORWARDED,
    )


@pytest.mark.unit
def test_partial_trust_fails_closed_behind_untrusted_intermediary(
    trusted_proxy_env: None,
) -> None:
    settings = get_settings()
    request = _request_with_client(
        "203.0.113.5",
        {"X-Forwarded-For": "203.0.113.50, 10.0.0.1"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution == ClientSourceResolution(
        "203.0.113.5",
        SourceResolutionPath.DIRECT_PEER,
    )


@pytest.mark.unit
def test_direct_render_origin_ignores_cloudflare_vendor_header(
    trusted_proxy_env: None,
) -> None:
    settings = get_settings()
    request = _request_with_client(
        "203.0.113.5",
        {"CF-Connecting-IP": "203.0.113.99", "X-Forwarded-For": "203.0.113.99"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.5"
    assert resolution.path is SourceResolutionPath.DIRECT_PEER


@pytest.mark.unit
def test_header_precedence_prefers_x_forwarded_for_over_forwarded(
    trusted_proxy_env: None,
) -> None:
    settings = get_settings()
    request = _request_with_client(
        "10.0.0.5",
        {
            "X-Forwarded-For": "203.0.113.10, 10.0.0.2",
            "Forwarded": 'for=203.0.113.99;proto=https, for=10.0.0.2',
            "CF-Connecting-IP": "203.0.113.88",
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.10"


@pytest.mark.unit
def test_forwarded_header_used_when_x_forwarded_for_missing(
    trusted_proxy_env: None,
) -> None:
    settings = get_settings()
    request = _request_with_client(
        "10.0.0.5",
        {"Forwarded": 'for="[2001:db8::7]";proto=https, for=10.0.0.2'},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "2001:db8::7"


@pytest.mark.unit
def test_invalid_and_overlong_forwarding_data_is_conservative(
    trusted_proxy_env: None,
) -> None:
    settings = get_settings()
    overlong = ", ".join(f"203.0.113.{index}" for index in range(40))
    request = _request_with_client("10.0.0.5", {"X-Forwarded-For": overlong})
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.path is SourceResolutionPath.DIRECT_PEER
    assert resolution.source == "10.0.0.5"

    malformed = _request_with_client("10.0.0.5", {"X-Forwarded-For": "not-an-ip"})
    malformed_resolution = resolve_admin_login_client_source(malformed, settings)
    assert malformed_resolution.path is SourceResolutionPath.DIRECT_PEER
    assert malformed_resolution.source == "10.0.0.5"


@pytest.mark.unit
def test_missing_peer_uses_unknown_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    settings = get_settings()
    scope = {
        "type": "http",
        "headers": [],
        "client": None,
        "method": "POST",
        "path": "/admin/login",
    }
    resolution = resolve_admin_login_client_source(Request(scope), settings)
    assert resolution == ClientSourceResolution("unknown", SourceResolutionPath.UNKNOWN_PEER)


@pytest.mark.unit
def test_untrusted_forwarding_attempt_emits_sampled_warning(
    trusted_proxy_env: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from app import proxy_trust

    proxy_trust.reset_proxy_trust_telemetry()
    settings = get_settings()
    request = _request_with_client(
        "203.0.113.5",
        {"X-Forwarded-For": "203.0.113.99"},
    )
    with caplog.at_level(logging.WARNING):
        resolve_admin_login_client_source(request, settings)
    assert "Ignored untrusted or invalid admin login forwarding headers" in caplog.text
    assert any(
        getattr(record, "source_resolution_path", None) == "untrusted_forwarded"
        for record in caplog.records
    )
    assert "203.0.113" not in caplog.text


@pytest.mark.unit
@pytest.mark.integration
def test_rotating_spoofed_headers_do_not_create_new_source_buckets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    monkeypatch.delenv("UVICORN_FORWARDED_ALLOW_IPS", raising=False)
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "5")
    admin_auth.reset_login_rate_limiter()
    store = FakeRateLimitStore()

    with shared_rate_limiter(store), mock_db_connection():
        for index in range(5):
            response = _login(
                username="ghost",
                password="wrong",
                headers={"X-Forwarded-For": f"203.0.113.{index}"},
            )
            assert response.status_code == 401

        blocked = _login(
            username="ghost",
            password="wrong",
            headers={"X-Forwarded-For": "203.0.113.99"},
        )
        assert blocked.status_code == 429
        assert len(store.rows) == 1


@pytest.mark.unit
@pytest.mark.integration
def test_trusted_proxy_headers_share_source_bucket(
    trusted_proxy_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    admin_auth.reset_login_rate_limiter()
    store = FakeRateLimitStore()
    settings = get_settings()

    with shared_rate_limiter(store):
        for _ in range(2):
            request = _request_with_client(
                "10.0.0.5",
                {"X-Forwarded-For": "203.0.113.77"},
            )
            assert admin_auth.try_admit_login_attempt(
                request, settings, username="ghost"
            ).admitted

        blocked_request = _request_with_client(
            "10.0.0.5",
            {"X-Forwarded-For": "203.0.113.77"},
        )
        blocked = admin_auth.try_admit_login_attempt(
            blocked_request, settings, username="ghost"
        )

    assert not blocked.admitted
    assert blocked.throttled
    assert len(store.rows) == 1


@pytest.mark.unit
def test_render_yaml_proxy_trust_settings_are_consistent() -> None:
    payload = yaml.safe_load(RENDER_YAML.read_text(encoding="utf-8"))
    service = payload["services"][0]
    start_command = service["startCommand"]
    env = {item["key"]: item.get("value") for item in service["envVars"] if "value" in item}

    assert "--forwarded-allow-ips" in start_command
    assert '"$UVICORN_FORWARDED_ALLOW_IPS"' in start_command
    assert env["UVICORN_FORWARDED_ALLOW_IPS"] == env["ADMIN_TRUSTED_PROXY_CIDRS"]
    assert "10.0.0.0/8" in env["UVICORN_FORWARDED_ALLOW_IPS"]


@pytest.mark.unit
def test_admin_auth_docs_describe_proxy_trust_model() -> None:
    docs = (REPO_ROOT / "docs" / "ADMIN_AUTH.md").read_text(encoding="utf-8")
    assert "UVICORN_FORWARDED_ALLOW_IPS" in docs
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in docs
    assert "right-to-left" in docs
    assert "CF-Connecting-IP" in docs


@pytest.mark.unit
@pytest.mark.integration
def test_uvicorn_proxy_headers_middleware_matches_app_resolver(
    trusted_proxy_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    admin_auth.reset_login_rate_limiter()
    store = FakeRateLimitStore()

    wrapped = ProxyHeadersMiddleware(app, trusted_hosts=TRUSTED_PROXIES)
    transport = httpx.ASGITransport(app=wrapped)
    shared_headers = {
        "X-Forwarded-For": "203.0.113.55, 203.0.113.77",
        "X-Forwarded-Host": "testserver",
    }

    async def _post_login(headers: dict[str, str]) -> httpx.Response:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as async_client:
            form = await async_client.get("/admin/login")
            csrf_match = re.search(r'name="csrf_token" value="([^"]+)"', form.text)
            assert csrf_match is not None
            return await async_client.post(
                "/admin/login",
                data={
                    "username": "ghost",
                    "password": "wrong",
                    "csrf_token": csrf_match.group(1),
                },
                headers=headers,
                cookies=form.cookies,
            )

    with shared_rate_limiter(store), mock_db_connection():
        first = asyncio.run(_post_login(shared_headers))
        second = asyncio.run(_post_login(shared_headers))
        blocked = asyncio.run(_post_login(shared_headers))

    assert first.status_code == 401
    assert second.status_code == 401
    assert blocked.status_code == 429
    assert len(store.rows) == 1


@pytest.mark.unit
@pytest.mark.integration
def test_privacy_no_raw_forwarding_data_in_limiter_or_logs(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    monkeypatch.delenv("UVICORN_FORWARDED_ALLOW_IPS", raising=False)
    admin_auth.reset_login_rate_limiter()
    store = FakeRateLimitStore()

    with shared_rate_limiter(store), mock_db_connection(), caplog.at_level(logging.INFO):
        response = _login(
            username="ghost",
            password="wrong",
            headers={"X-Forwarded-For": "203.0.113.44"},
        )

    assert response.status_code == 401
    assert len(store.rows) == 1
    limiter_key = next(iter(store.rows))
    assert "203.0.113" not in limiter_key
    assert len(limiter_key) == 64
    assert "203.0.113" not in caplog.text
    assert "x-forwarded-for" not in caplog.text.lower()
    assert any(
        getattr(record, "source_resolution_path", None)
        for record in caplog.records
        if record.name == "app.admin_auth"
    )
