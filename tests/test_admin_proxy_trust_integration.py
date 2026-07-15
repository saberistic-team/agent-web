"""Integration tests for admin login limiter + proxy trust through the ASGI stack."""

from __future__ import annotations

from unittest.mock import patch

import pytest

pytest_plugins = ["tests.test_admin_auth"]
from fastapi.testclient import TestClient

from app import admin_auth
from app.main import app
from tests.test_admin_auth import (
    FakeRateLimitStore,
    TEST_USERNAME,
    _fetch_login_form,
    mock_db_connection,
    shared_rate_limiter,
)

RENDER_PROXY = "10.0.0.1"
CLOUDFLARE_EDGE = "172.64.0.10"
CLIENT_A = "203.0.113.50"
SPOOF = "203.0.113.99"


class _ClientHostMiddleware:
    def __init__(self, inner_app, client_host: str) -> None:
        self.inner_app = inner_app
        self.client_host = client_host

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            scope = {**scope, "client": (self.client_host, 0)}
        await self.inner_app(scope, receive, send)


def _client_with_peer(client_host: str) -> TestClient:
    wrapped = _ClientHostMiddleware(app, client_host)
    return TestClient(wrapped, follow_redirects=False)


@pytest.mark.unit
@pytest.mark.integration
def test_trusted_hop_login_limiter_through_asgi_stack(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full admin login path with a simulated Render proxy peer."""
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", f"{RENDER_PROXY}/32")
    monkeypatch.setenv("ADMIN_CLOUDFLARE_EDGE_CIDRS", f"{CLOUDFLARE_EDGE}/32")
    monkeypatch.setenv("ADMIN_TRUST_CLOUDFLARE_EDGE", "true")
    monkeypatch.setenv("UVICORN_FORWARDED_ALLOW_IPS", "")

    test_client = _client_with_peer(RENDER_PROXY)

    def _post(headers: dict[str, str]) -> int:
        csrf_token, cookies = _fetch_login_form()
        response = test_client.post(
            "/admin/login",
            data={
                "username": "ghost",
                "password": "wrong",
                "csrf_token": csrf_token,
            },
            cookies=cookies,
            headers=headers,
        )
        return response.status_code

    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            headers = {"X-Forwarded-For": f"{SPOOF}, {CLIENT_A}, {CLOUDFLARE_EDGE}"}
            assert _post(headers) == 401
            assert _post(headers) == 401
            assert _post(headers) == 429

            rotated = {"X-Forwarded-For": f"203.0.113.1, {CLIENT_A}, {CLOUDFLARE_EDGE}"}
            assert _post(rotated) == 429

    source_key = admin_auth.build_source_rate_limit_key(CLIENT_A)
    assert len(rate_limit_store.rows) == 1
    assert source_key in rate_limit_store.rows


@pytest.mark.unit
@pytest.mark.integration
def test_rotating_spoofed_headers_does_not_create_new_limiter_rows(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "3")

    test_client = TestClient(app, follow_redirects=False)

    def _post(headers: dict[str, str]) -> int:
        csrf_token, cookies = _fetch_login_form()
        response = test_client.post(
            "/admin/login",
            data={
                "username": "ghost",
                "password": "wrong",
                "csrf_token": csrf_token,
            },
            cookies=cookies,
            headers=headers,
        )
        return response.status_code

    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            for index in range(3):
                headers = {"X-Forwarded-For": f"203.0.113.{index}"}
                assert _post(headers) == 401
            assert _post({"X-Forwarded-For": "203.0.113.50"}) == 429

    assert len(rate_limit_store.rows) == 1
    source_key = admin_auth.build_source_rate_limit_key("testclient")
    assert source_key in rate_limit_store.rows


@pytest.mark.unit
@pytest.mark.integration
def test_limiter_and_audit_state_contain_no_raw_forwarding_data(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    test_client = _client_with_peer(RENDER_PROXY)
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", f"{RENDER_PROXY}/32")
    monkeypatch.setenv("ADMIN_CLOUDFLARE_EDGE_CIDRS", f"{CLOUDFLARE_EDGE}/32")

    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection(), patch("app.admin_routes._record_login_failure") as audit_mock:
            csrf_token, cookies = _fetch_login_form()
            response = test_client.post(
                "/admin/login",
                data={
                    "username": TEST_USERNAME,
                    "password": "wrong",
                    "csrf_token": csrf_token,
                },
                cookies=cookies,
                headers={"X-Forwarded-For": f"{SPOOF}, {CLIENT_A}, {CLOUDFLARE_EDGE}"},
            )
            assert response.status_code == 401

    serialized = str(rate_limit_store.rows)
    assert SPOOF not in serialized
    assert CLIENT_A not in serialized
    assert "x-forwarded-for" not in serialized.lower()

    if audit_mock.called:
        audit_payload = str(audit_mock.call_args.kwargs)
        assert SPOOF not in audit_payload
        assert CLIENT_A not in audit_payload


@pytest.mark.unit
def test_uvicorn_forwarded_allow_ips_flag_documented_in_render_yaml() -> None:
    from pathlib import Path

    text = Path("render.yaml").read_text()
    assert "--forwarded-allow-ips=" in text
