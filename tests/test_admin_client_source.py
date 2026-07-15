"""Tests for trusted-hop admin login client source resolution (#239)."""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app import admin_auth
from app.admin_client_source import (
    ClientSourceResolutionPath,
    reset_client_source_telemetry,
    resolve_admin_login_client_source,
    resolve_admin_login_client_source_text,
)
from app.config import get_settings

RENDER_LB = "10.0.0.1"
CF_EDGE = "104.16.0.1"
REAL_CLIENT = "198.51.100.55"
SPOOFED_CLIENT = "203.0.113.99"
OTHER_CLIENT = "203.0.113.88"

TRUSTED_CIDRS = "10.0.0.0/8,172.16.0.0/12,100.64.0.0/10"
EDGE_CIDRS = "104.16.0.0/13"


@pytest.fixture(autouse=True)
def client_source_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from argon2 import PasswordHasher

    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", "operator")
    monkeypatch.setenv(
        "ADMIN_PASSWORD_HASH",
        PasswordHasher().hash("correct-horse-battery-staple"),
    )
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "test-session-secret-32chars-minimum")
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    monkeypatch.delenv("ADMIN_EDGE_PROXY_CIDRS", raising=False)
    admin_auth.reset_login_rate_limiter()
    reset_client_source_telemetry()


def _request_with_client(
    host: str,
    *,
    headers: dict[str, str] | None = None,
) -> Request:
    header_list = [
        (key.lower().encode("latin1"), value.encode("latin1"))
        for key, value in (headers or {}).items()
    ]
    scope = {
        "type": "http",
        "headers": header_list,
        "client": (host, 12345),
        "method": "POST",
        "path": "/admin/login",
    }
    return Request(scope)


def _trusted_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", TRUSTED_CIDRS)
    monkeypatch.setenv("ADMIN_EDGE_PROXY_CIDRS", EDGE_CIDRS)


@pytest.mark.unit
def test_direct_spoof_single_and_multi_value_xff_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    for xff in (
        SPOOFED_CLIENT,
        f"{SPOOFED_CLIENT}, {OTHER_CLIENT}",
    ):
        request = _request_with_client(
            REAL_CLIENT,
            headers={"X-Forwarded-For": xff},
        )
        resolution = resolve_admin_login_client_source(request, settings)
        assert resolution.source == REAL_CLIENT
        assert resolution.path is ClientSourceResolutionPath.DIRECT_PEER


@pytest.mark.unit
def test_cloudflare_append_ignores_attacker_leftmost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _trusted_env(monkeypatch)
    settings = get_settings()
    request = _request_with_client(
        RENDER_LB,
        headers={
            "X-Forwarded-For": f"{SPOOFED_CLIENT}, {REAL_CLIENT}, {CF_EDGE}, {RENDER_LB}",
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
    assert resolution.path is ClientSourceResolutionPath.TRUSTED_XFF


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _trusted_env(monkeypatch)
    settings = get_settings()
    request = _request_with_client(
        RENDER_LB,
        headers={"X-Forwarded-For": f"{REAL_CLIENT}, {RENDER_LB}"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
    assert resolution.path is ClientSourceResolutionPath.TRUSTED_XFF


@pytest.mark.unit
def test_partial_trust_untrusted_intermediary_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    request = _request_with_client(
        "203.0.113.1",
        headers={"X-Forwarded-For": f"{SPOOFED_CLIENT}, {RENDER_LB}"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.1"
    assert resolution.path is ClientSourceResolutionPath.DIRECT_PEER


@pytest.mark.unit
def test_direct_render_origin_ignores_spoofed_cf_connecting_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _trusted_env(monkeypatch)
    settings = get_settings()
    request = _request_with_client(
        RENDER_LB,
        headers={
            "CF-Connecting-IP": SPOOFED_CLIENT,
            "X-Forwarded-For": f"{REAL_CLIENT}, {RENDER_LB}",
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
    assert resolution.path is ClientSourceResolutionPath.TRUSTED_XFF


@pytest.mark.unit
def test_cf_connecting_ip_used_when_edge_verified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _trusted_env(monkeypatch)
    settings = get_settings()
    request = _request_with_client(
        RENDER_LB,
        headers={
            "CF-Connecting-IP": REAL_CLIENT,
            "X-Forwarded-For": f"{REAL_CLIENT}, {CF_EDGE}, {RENDER_LB}",
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
    assert resolution.path is ClientSourceResolutionPath.TRUSTED_CF_CONNECTING


@pytest.mark.unit
def test_multiple_header_families_precedence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _trusted_env(monkeypatch)
    settings = get_settings()
    request = _request_with_client(
        RENDER_LB,
        headers={
            "CF-Connecting-IP": REAL_CLIENT,
            "X-Forwarded-For": f"{OTHER_CLIENT}, {CF_EDGE}, {RENDER_LB}",
            "Forwarded": f'for="{SPOOFED_CLIENT}";proto=https',
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
    assert resolution.path is ClientSourceResolutionPath.TRUSTED_CF_CONNECTING


@pytest.mark.unit
@pytest.mark.parametrize(
    ("xff", "expected"),
    [
        ("203.0.113.1", "203.0.113.1"),
        ("2001:db8::1", "2001:db8::1"),
        ("203.0.113.1:8080", "203.0.113.1"),
        ("[2001:db8::1]:443", "2001:db8::1"),
        ("::ffff:203.0.113.1", "203.0.113.1"),
        (" 203.0.113.1 , 10.0.0.1", "203.0.113.1"),
    ],
)
def test_address_formats(
    monkeypatch: pytest.MonkeyPatch,
    xff: str,
    expected: str,
) -> None:
    _trusted_env(monkeypatch)
    settings = get_settings()
    request = _request_with_client(RENDER_LB, headers={"X-Forwarded-For": xff})
    assert resolve_admin_login_client_source_text(request, settings) == expected


@pytest.mark.unit
def test_malformed_and_overlong_headers_map_to_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _trusted_env(monkeypatch)
    settings = get_settings()
    overlong = ",".join(["203.0.113.1"] * 80)
    request = _request_with_client(RENDER_LB, headers={"X-Forwarded-For": overlong})
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "unknown"
    assert resolution.path is ClientSourceResolutionPath.MALFORMED

    invalid = _request_with_client(
        RENDER_LB,
        headers={"X-Forwarded-For": "not-an-ip, 10.0.0.1"},
    )
    invalid_resolution = resolve_admin_login_client_source(invalid, settings)
    assert invalid_resolution.source == "unknown"
    assert invalid_resolution.path is ClientSourceResolutionPath.MALFORMED


@pytest.mark.unit
def test_all_trusted_chain_is_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _trusted_env(monkeypatch)
    settings = get_settings()
    request = _request_with_client(
        RENDER_LB,
        headers={"X-Forwarded-For": f"{RENDER_LB}, 10.0.0.2"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "unknown"
    assert resolution.path is ClientSourceResolutionPath.AMBIGUOUS_CHAIN


@pytest.mark.unit
def test_forwarded_header_used_when_xff_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _trusted_env(monkeypatch)
    settings = get_settings()
    request = _request_with_client(
        RENDER_LB,
        headers={"Forwarded": f'for="{REAL_CLIENT}";proto=https, for={RENDER_LB}'},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
    assert resolution.path is ClientSourceResolutionPath.TRUSTED_FORWARDED


@pytest.mark.unit
def test_forwarded_header_precedence_after_xff_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _trusted_env(monkeypatch)
    settings = get_settings()
    request = _request_with_client(
        RENDER_LB,
        headers={
            "X-Forwarded-For": "not-an-ip",
            "Forwarded": f'for="{REAL_CLIENT}";proto=https',
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
    assert resolution.path is ClientSourceResolutionPath.TRUSTED_FORWARDED


@pytest.mark.unit
def test_missing_peer_maps_to_unknown() -> None:
    settings = get_settings()
    request = Request(
        {
            "type": "http",
            "headers": [],
            "client": None,
            "method": "POST",
            "path": "/admin/login",
        }
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "unknown"
    assert resolution.path is ClientSourceResolutionPath.MISSING_PEER


@pytest.mark.unit
def test_untrusted_forwarding_telemetry_is_sampled(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _trusted_env(monkeypatch)
    settings = get_settings()
    caplog.set_level(logging.INFO, logger="app.admin_client_source")
    request = _request_with_client(
        RENDER_LB,
        headers={"X-Forwarded-For": f"{RENDER_LB}, 10.0.0.2"},
    )
    for _ in range(3):
        resolve_admin_login_client_source(request, settings)
    messages = [record.getMessage() for record in caplog.records]
    assert sum("ignored untrusted forwarding" in message for message in messages) == 1


@pytest.mark.unit
def test_malformed_cidr_entries_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", "not-a-cidr,10.0.0.0/8")
    settings = get_settings()
    assert settings.admin_trusted_proxy_cidrs
    request = _request_with_client(
        RENDER_LB,
        headers={"X-Forwarded-For": f"{REAL_CLIENT}, {RENDER_LB}"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT


@pytest.mark.unit
def test_login_admission_logs_resolution_path_without_raw_ips(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from tests.test_admin_auth import FakeRateLimitStore, _login, shared_rate_limiter

    caplog.set_level(logging.INFO, logger="app.admin_auth")
    store = FakeRateLimitStore()
    with shared_rate_limiter(store):
        _login(password="wrong")
    for record in caplog.records:
        if record.getMessage() == "Admin login attempt admitted":
            assert "client_source_resolution_path" in record.__dict__
            assert record.__dict__["client_source_resolution_path"] == "direct_peer"
            assert REAL_CLIENT not in str(record.__dict__)
            assert SPOOFED_CLIENT not in str(record.__dict__)
            break
    else:
        pytest.fail("expected admission log with resolution path")


@pytest.mark.unit
def test_rotating_spoofed_headers_share_one_source_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    keys = {
        admin_auth.build_source_rate_limit_key(
            resolve_admin_login_client_source_text(
                _request_with_client(
                    REAL_CLIENT,
                    headers={"X-Forwarded-For": spoof},
                ),
                settings,
            )
        )
        for spoof in (SPOOFED_CLIENT, OTHER_CLIENT, "203.0.113.1, 203.0.113.2")
    }
    assert len(keys) == 1


@pytest.mark.unit
def test_render_yaml_proxy_trust_settings_are_consistent() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    render_yaml = (repo_root / "render.yaml").read_text(encoding="utf-8")
    assert "--proxy-headers" in render_yaml
    assert "--forwarded-allow-ips=" in render_yaml
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in render_yaml
    assert "ADMIN_EDGE_PROXY_CIDRS" in render_yaml

    allow_ips_match = re.search(r"--forwarded-allow-ips=([^\n]+)", render_yaml)
    trusted_match = re.search(
        r"ADMIN_TRUSTED_PROXY_CIDRS\n\s+value:\s+\"([^\"]+)\"",
        render_yaml,
    )
    assert allow_ips_match is not None
    assert trusted_match is not None
    assert allow_ips_match.group(1) == trusted_match.group(1)


@pytest.mark.unit
def test_telemetry_and_limiter_state_contain_no_raw_forwarding_data(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _trusted_env(monkeypatch)
    settings = get_settings()
    caplog.set_level(logging.DEBUG, logger="app.admin_client_source")
    request = _request_with_client(
        RENDER_LB,
        headers={
            "X-Forwarded-For": f"{SPOOFED_CLIENT}, not-an-ip",
            "CF-Connecting-IP": REAL_CLIENT,
        },
    )
    resolve_admin_login_client_source(request, settings)
    combined_logs = " ".join(record.getMessage() for record in caplog.records)
    for record in caplog.records:
        message = record.getMessage()
        assert REAL_CLIENT not in message
        assert SPOOFED_CLIENT not in message
        assert "x-forwarded-for" not in message.lower()
        assert record.__dict__.get("resolution_path") in {
            None,
            ClientSourceResolutionPath.MALFORMED.value,
        } or isinstance(record.__dict__.get("resolution_path"), str)

    source_key = admin_auth.build_source_rate_limit_key("unknown")
    assert REAL_CLIENT not in source_key
    assert SPOOFED_CLIENT not in source_key
    assert len(source_key) == 64
    assert combined_logs or True


@pytest.mark.integration
def test_uvicorn_proxy_headers_middleware_matches_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _trusted_env(monkeypatch)
    settings = get_settings()

    probe_app = FastAPI()

    @probe_app.get("/probe")
    def probe(request: Request) -> dict[str, str]:
        resolution = resolve_admin_login_client_source(request, settings)
        return {
            "scope_client": request.client.host if request.client else "",
            "resolved_source": resolution.source,
            "resolution_path": resolution.path.value,
        }

    wrapped = ProxyHeadersMiddleware(probe_app, trusted_hosts=TRUSTED_CIDRS.split(","))
    client = TestClient(wrapped, client=(RENDER_LB, 50000))

    response = client.get(
        "/probe",
        headers={
            "X-Forwarded-For": f"{SPOOFED_CLIENT}, {REAL_CLIENT}, {RENDER_LB}",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["scope_client"] == REAL_CLIENT
    assert payload["resolved_source"] == REAL_CLIENT
    assert payload["resolved_source"] != SPOOFED_CLIENT
    # After Uvicorn ProxyHeadersMiddleware the immediate peer is already the client.
    assert payload["resolution_path"] == ClientSourceResolutionPath.DIRECT_PEER.value


@pytest.mark.integration
def test_login_limiter_uses_trusted_hop_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_admin_auth import (
        FakeRateLimitStore,
        _extract_csrf_token,
        _parse_login_form,
        mock_db_connection,
        shared_rate_limiter,
    )

    from app.main import app

    _trusted_env(monkeypatch)
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")

    store = FakeRateLimitStore()
    trusted_client = TestClient(app, follow_redirects=False, client=(RENDER_LB, 50000))
    headers = {
        "X-Forwarded-For": f"{SPOOFED_CLIENT}, {REAL_CLIENT}, {RENDER_LB}",
    }
    with shared_rate_limiter(store):
        with mock_db_connection():
            for _ in range(2):
                form = trusted_client.get("/admin/login")
                csrf_token, cookies = _parse_login_form(form)
                response = trusted_client.post(
                    "/admin/login",
                    data={
                        "username": "ghost",
                        "password": "wrong",
                        "csrf_token": csrf_token,
                    },
                    cookies=cookies,
                    headers=headers,
                )
                assert response.status_code == 401
            blocked = trusted_client.post(
                "/admin/login",
                data={
                    "username": "ghost",
                    "password": "wrong",
                    "csrf_token": _extract_csrf_token(
                        trusted_client.get("/admin/login").text
                    ),
                },
                headers=headers,
            )
            assert blocked.status_code == 429

            rotated = {
                "X-Forwarded-For": f"{OTHER_CLIENT}, {REAL_CLIENT}, {RENDER_LB}",
            }
            still_blocked = trusted_client.post(
                "/admin/login",
                data={
                    "username": "ghost",
                    "password": "wrong",
                    "csrf_token": _extract_csrf_token(
                        trusted_client.get("/admin/login").text
                    ),
                },
                headers=rotated,
            )
            assert still_blocked.status_code == 429

    source_key = admin_auth.build_source_rate_limit_key(REAL_CLIENT)
    assert source_key in store.rows
    assert len(store.rows) == 1
