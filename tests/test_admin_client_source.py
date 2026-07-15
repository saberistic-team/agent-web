"""Tests for trusted admin login client source resolution (#239)."""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest
import uvicorn
from fastapi import Request
from fastapi.testclient import TestClient

from app import admin_auth
from app.config import get_settings
from app.main import app
from app.trusted_proxy import (
    MISSING_CLIENT_SOURCE,
    default_trusted_proxy_cidrs,
    reset_source_resolution_telemetry,
    resolve_client_source,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
RENDER_PROXY = "10.0.0.1"
RENDER_PROXY_ALT = "10.0.0.2"
REAL_CLIENT = "203.0.113.77"
SPOOFED_CLIENT = "198.51.100.99"
CLOUDFLARE_EDGE = "162.158.1.1"
DIRECT_PEER = "198.51.100.10"

client = TestClient(app, follow_redirects=False)


def _request_with_client(
    host: str,
    *,
    headers: list[tuple[bytes, bytes]] | None = None,
) -> Request:
    scope = {
        "type": "http",
        "headers": headers or [],
        "client": (host, 12345),
        "method": "POST",
        "path": "/admin/login",
    }
    return Request(scope)


def _headers(**pairs: str) -> list[tuple[bytes, bytes]]:
    return [(key.lower().encode("ascii"), value.encode("ascii")) for key, value in pairs.items()]


@pytest.fixture(autouse=True)
def reset_telemetry() -> None:
    reset_source_resolution_telemetry()


@pytest.mark.unit
def test_default_trusted_proxy_cidrs_include_render_and_cloudflare() -> None:
    cidrs = default_trusted_proxy_cidrs()
    assert "10.0.0.0/8" in cidrs
    assert "162.158.0.0/15" in cidrs


@pytest.mark.unit
def test_direct_spoof_single_value_xff_ignored() -> None:
    resolution = resolve_client_source(
        peer_host=DIRECT_PEER,
        headers={"X-Forwarded-For": SPOOFED_CLIENT},
        trust_proxy_headers=True,
        trusted_proxy_cidrs=("10.0.0.0/8",),
    )
    assert resolution.source == DIRECT_PEER
    assert resolution.path == "direct_peer"
    assert resolution.rejected_forwarding is True


@pytest.mark.unit
def test_direct_spoof_multi_value_xff_ignored() -> None:
    resolution = resolve_client_source(
        peer_host=DIRECT_PEER,
        headers={"X-Forwarded-For": f"{SPOOFED_CLIENT}, {REAL_CLIENT}, {RENDER_PROXY}"},
        trust_proxy_headers=True,
        trusted_proxy_cidrs=("10.0.0.0/8",),
    )
    assert resolution.source == DIRECT_PEER
    assert resolution.path == "direct_peer"


@pytest.mark.unit
def test_cloudflare_append_selects_real_client_not_leftmost() -> None:
    resolution = resolve_client_source(
        peer_host=RENDER_PROXY,
        headers={
            "X-Forwarded-For": f"{SPOOFED_CLIENT}, {REAL_CLIENT}, {CLOUDFLARE_EDGE}",
        },
        trust_proxy_headers=True,
        trusted_proxy_cidrs=default_trusted_proxy_cidrs(),
    )
    assert resolution.source == REAL_CLIENT
    assert resolution.path == "xff_trusted_walk"


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client() -> None:
    resolution = resolve_client_source(
        peer_host=RENDER_PROXY,
        headers={"X-Forwarded-For": f"{REAL_CLIENT}, {RENDER_PROXY}"},
        trust_proxy_headers=True,
        trusted_proxy_cidrs=("10.0.0.0/8",),
    )
    assert resolution.source == REAL_CLIENT
    assert resolution.path == "xff_trusted_walk"


@pytest.mark.unit
def test_partial_trust_fails_closed_behind_untrusted_intermediary() -> None:
    resolution = resolve_client_source(
        peer_host="203.0.113.5",
        headers={"X-Forwarded-For": f"{REAL_CLIENT}, {RENDER_PROXY}"},
        trust_proxy_headers=True,
        trusted_proxy_cidrs=("10.0.0.0/8",),
    )
    assert resolution.source == "203.0.113.5"
    assert resolution.path == "direct_peer"
    assert resolution.rejected_forwarding is True


@pytest.mark.unit
def test_direct_render_origin_ignores_unproven_cf_connecting_ip() -> None:
    resolution = resolve_client_source(
        peer_host=RENDER_PROXY,
        headers={
            "CF-Connecting-IP": SPOOFED_CLIENT,
            "X-Forwarded-For": REAL_CLIENT,
        },
        trust_proxy_headers=True,
        trusted_proxy_cidrs=("10.0.0.0/8",),
    )
    assert resolution.source == REAL_CLIENT
    assert resolution.path == "xff_trusted_walk"


@pytest.mark.unit
def test_cf_connecting_ip_used_when_cloudflare_edge_proven() -> None:
    resolution = resolve_client_source(
        peer_host=RENDER_PROXY,
        headers={
            "CF-Connecting-IP": REAL_CLIENT,
            "CF-Ray": "abc123-LHR",
            "X-Forwarded-For": f"{SPOOFED_CLIENT}, {CLOUDFLARE_EDGE}",
        },
        trust_proxy_headers=True,
        trusted_proxy_cidrs=default_trusted_proxy_cidrs(),
    )
    assert resolution.source == REAL_CLIENT
    assert resolution.path == "cf_connecting_ip"


@pytest.mark.unit
def test_header_precedence_cf_connecting_ip_over_conflicting_xff() -> None:
    resolution = resolve_client_source(
        peer_host=RENDER_PROXY,
        headers={
            "CF-Connecting-IP": REAL_CLIENT,
            "CF-Ray": "edge-proof",
            "X-Forwarded-For": f"{SPOOFED_CLIENT}, {RENDER_PROXY}",
            "Forwarded": f'for="{SPOOFED_CLIENT}";proto=https',
        },
        trust_proxy_headers=True,
        trusted_proxy_cidrs=default_trusted_proxy_cidrs(),
    )
    assert resolution.source == REAL_CLIENT
    assert resolution.path == "cf_connecting_ip"


@pytest.mark.unit
def test_forwarded_header_used_when_xff_absent() -> None:
    resolution = resolve_client_source(
        peer_host=RENDER_PROXY,
        headers={"Forwarded": f'for="{REAL_CLIENT}";proto=https, for="{RENDER_PROXY}"'},
        trust_proxy_headers=True,
        trusted_proxy_cidrs=("10.0.0.0/8",),
    )
    assert resolution.source == REAL_CLIENT
    assert resolution.path == "forwarded_trusted_walk"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("203.0.113.1", "203.0.113.1"),
        ("2001:db8::1", "2001:db8::1"),
        ("::ffff:203.0.113.1", "203.0.113.1"),
        (" 203.0.113.1 ", "203.0.113.1"),
        ("203.0.113.1:12345", "203.0.113.1"),
        ("[2001:db8::1]:443", "2001:db8::1"),
    ],
)
def test_address_formats_normalize_deterministically(raw: str, expected: str) -> None:
    resolution = resolve_client_source(
        peer_host=RENDER_PROXY,
        headers={"X-Forwarded-For": raw},
        trust_proxy_headers=True,
        trusted_proxy_cidrs=("10.0.0.0/8",),
    )
    assert resolution.source == expected


@pytest.mark.unit
def test_invalid_and_overlong_forwarding_fails_closed() -> None:
    overlong = ", ".join([f"203.0.113.{index}" for index in range(40)])
    resolution = resolve_client_source(
        peer_host=RENDER_PROXY,
        headers={"X-Forwarded-For": overlong},
        trust_proxy_headers=True,
        trusted_proxy_cidrs=("10.0.0.0/8",),
    )
    assert resolution.source == RENDER_PROXY
    assert resolution.path == "malformed_forwarding"

    malformed = resolve_client_source(
        peer_host=RENDER_PROXY,
        headers={"X-Forwarded-For": "not-an-ip"},
        trust_proxy_headers=True,
        trusted_proxy_cidrs=("10.0.0.0/8",),
    )
    assert malformed.source == RENDER_PROXY
    assert malformed.path == "malformed_forwarding"


@pytest.mark.unit
def test_missing_peer_uses_unknown_bucket() -> None:
    resolution = resolve_client_source(
        peer_host=None,
        headers={"X-Forwarded-For": REAL_CLIENT},
        trust_proxy_headers=True,
        trusted_proxy_cidrs=("10.0.0.0/8",),
    )
    assert resolution.source == MISSING_CLIENT_SOURCE
    assert resolution.path == "missing_peer"


@pytest.mark.unit
def test_telemetry_logs_path_without_raw_addresses(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="app.trusted_proxy")
    resolve_client_source(
        peer_host=DIRECT_PEER,
        headers={"X-Forwarded-For": SPOOFED_CLIENT},
        trust_proxy_headers=True,
        trusted_proxy_cidrs=("10.0.0.0/8",),
    )
    assert SPOOFED_CLIENT not in caplog.text
    assert DIRECT_PEER not in caplog.text
    assert "rejected forwarding headers" in caplog.text
    assert "source_resolution_path" not in caplog.text


@pytest.mark.unit
def test_resolve_admin_login_client_source_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    settings = get_settings()
    request = _request_with_client(
        RENDER_PROXY,
        headers=_headers(**{"X-Forwarded-For": f"{REAL_CLIENT}, {RENDER_PROXY}"}),
    )
    resolution = admin_auth.resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
    assert admin_auth.client_ip(request, settings) == REAL_CLIENT


@pytest.mark.unit
def test_render_yaml_declares_proxy_trust_settings() -> None:
    render_yaml = (REPO_ROOT / "render.yaml").read_text(encoding="utf-8")
    assert "bash scripts/run_uvicorn.sh" in render_yaml
    assert "ADMIN_TRUST_PROXY_HEADERS" in render_yaml
    assert 'value: "true"' in render_yaml
    assert "FORWARDED_ALLOW_IPS" in render_yaml
    assert 'value: "127.0.0.1"' in render_yaml


@pytest.mark.unit
def test_run_uvicorn_script_declares_forwarded_allow_ips() -> None:
    script = (REPO_ROOT / "scripts" / "run_uvicorn.sh").read_text(encoding="utf-8")
    assert "--forwarded-allow-ips" in script
    assert 'FORWARDED_ALLOW_IPS="${FORWARDED_ALLOW_IPS:-127.0.0.1}"' in script


@pytest.mark.unit
def test_admin_auth_docs_describe_trusted_hop_model() -> None:
    docs = (REPO_ROOT / "docs" / "ADMIN_AUTH.md").read_text(encoding="utf-8")
    assert "right-to-left" in docs
    assert "CF-Ray" in docs
    assert "FORWARDED_ALLOW_IPS" in docs
    assert "resolve_admin_login_client_source" in docs


@pytest.mark.unit
def test_limiter_keys_contain_no_raw_forwarding_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    settings = get_settings()
    request = _request_with_client(
        RENDER_PROXY,
        headers=_headers(**{"X-Forwarded-For": f"{REAL_CLIENT}, {RENDER_PROXY}"}),
    )
    source = admin_auth.client_ip(request, settings)
    keys = admin_auth.login_limiter_keys(
        submitted_username="ghost",
        client_source=source,
        configured_admin_username=settings.admin_username,
    )
    for key in keys:
        assert REAL_CLIENT not in key
        assert RENDER_PROXY not in key
        assert len(key) == 64


@pytest.mark.unit
def test_health_reports_proxy_trust_metadata() -> None:
    response = client.get("/health")
    payload = response.json()
    proxy_trust = payload["admin_proxy_trust"]
    assert proxy_trust["enabled"] is False
    assert proxy_trust["forwarded_allow_ips"] == "127.0.0.1"
    assert proxy_trust["trusted_proxy_cidrs_configured"] is True
    assert proxy_trust["default_trusted_proxy_cidr_count"] > 0


class FakeRateLimitStore:
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
        from datetime import timedelta

        from app import db

        for limiter_key in limiter_keys:
            row = self.rows.setdefault(
                limiter_key,
                {"failure_count": 0, "locked_until": None, "window_started_at": now},
            )
            locked_until = row.get("locked_until")
            if locked_until is not None and locked_until > now:
                return db.AdminLoginAdmission(
                    admitted=False,
                    throttled=True,
                    already_locked=True,
                    lockout_transition=False,
                )

        lockout_transition = False
        for limiter_key in limiter_keys:
            row = self.rows[limiter_key]
            row["failure_count"] += 1
            if row["failure_count"] >= rate_limit:
                row["locked_until"] = now + timedelta(seconds=lockout_seconds)
                if row["failure_count"] == rate_limit:
                    lockout_transition = True

        return db.AdminLoginAdmission(
            admitted=True,
            throttled=False,
            already_locked=False,
            lockout_transition=lockout_transition,
        )


@pytest.mark.unit
@pytest.mark.integration
def test_rotating_spoofed_headers_do_not_create_new_limiter_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import datetime, timezone

    from app import db

    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    settings = get_settings()
    store = FakeRateLimitStore()
    now = datetime.now(timezone.utc)

    def try_admit(
        conn: Any,
        *,
        limiter_keys: tuple[str, ...],
        now: datetime,
        rate_limit: int,
        window_seconds: int,
        lockout_seconds: int,
    ) -> db.AdminLoginAdmission:
        return store.try_admit(
            limiter_keys,
            now,
            rate_limit=rate_limit,
            window_seconds=window_seconds,
            lockout_seconds=lockout_seconds,
        )

    with (
        patch("app.admin_auth.db.try_admit_admin_login", side_effect=try_admit),
        patch("app.admin_auth.db.cleanup_expired_admin_login_rate_limits", return_value=0),
        patch("app.admin_auth.db.db_connection") as db_conn,
    ):
        db_conn.return_value.__enter__.return_value = object()
        db_conn.return_value.__exit__.return_value = None
        for index in range(3):
            request = _request_with_client(
                RENDER_PROXY,
                headers=_headers(
                    **{
                        "X-Forwarded-For": (
                            f"203.0.113.{index}, {REAL_CLIENT}, {CLOUDFLARE_EDGE}, {RENDER_PROXY}"
                        )
                    }
                ),
            )
            admission = admin_auth.try_admit_login_attempt(request, settings, username="ghost")
            if index < 2:
                assert admission.admitted is True
            else:
                assert admission.admitted is False

    assert len(store.rows) == 1


@pytest.mark.integration
def test_uvicorn_forwarded_allow_ips_matches_deployment_and_blocks_spoofed_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise deployment Uvicorn settings plus application resolver together."""

    class _PeerInjectMiddleware:
        def __init__(self, app: Any) -> None:
            self.app = app

        async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
            if scope["type"] == "http":
                scope["client"] = (RENDER_PROXY, 0)
            await self.app(scope, receive, send)

    from fastapi import FastAPI

    probe_app = FastAPI()

    @probe_app.get("/probe-source")
    def probe_source(request: Request) -> dict[str, str]:
        settings = get_settings()
        resolution = admin_auth.resolve_admin_login_client_source(request, settings)
        return {"source": resolution.source, "path": resolution.path}

    wrapped_app = _PeerInjectMiddleware(probe_app)
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    get_settings()

    config = uvicorn.Config(
        wrapped_app,
        host="127.0.0.1",
        port=0,
        log_level="warning",
        forwarded_allow_ips="127.0.0.1",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started
    assert server.servers
    port = server.servers[0].sockets[0].getsockname()[1]

    with httpx.Client(timeout=2.0) as http_client:
        response = http_client.get(
            f"http://127.0.0.1:{port}/probe-source",
            headers={
                "X-Forwarded-For": f"{SPOOFED_CLIENT}, {REAL_CLIENT}, {RENDER_PROXY}",
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["source"] == REAL_CLIENT
    assert payload["path"] == "xff_trusted_walk"

    server.should_exit = True
    thread.join(timeout=5)
