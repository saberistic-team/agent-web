"""Integration tests exercising Uvicorn proxy middleware + admin client source."""

from __future__ import annotations

import json

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app import admin_auth
from app.admin_client_source import default_trusted_proxy_cidrs
from app.config import get_settings
from app.main import app

TRUSTED_CIDRS = default_trusted_proxy_cidrs()
PROXY_APP = ProxyHeadersMiddleware(app, trusted_hosts=TRUSTED_CIDRS)


def _request(peer: str, headers: dict[str, str]) -> Request:
    scope = {
        "type": "http",
        "headers": [
            (key.lower().encode("ascii"), value.encode("ascii"))
            for key, value in headers.items()
        ],
        "client": (peer, 50000),
        "method": "GET",
        "path": "/health",
    }
    return Request(scope)


@pytest.mark.integration
def test_proxy_middleware_health_reports_proxy_trust_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", TRUSTED_CIDRS)
    monkeypatch.setenv("DATABASE_URL", "")
    client = TestClient(PROXY_APP, follow_redirects=False)
    response = client.get("/health")
    payload = json.loads(response.text)
    assert payload["status"] == "ok"
    assert payload["proxy_trust"]["client_source_trust_configured"] is True


@pytest.mark.integration
def test_proxy_middleware_trusted_xff_without_cf_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", TRUSTED_CIDRS)
    settings = get_settings()
    request = _request(
        "10.0.0.2",
        {"X-Forwarded-For": "203.0.113.10, 10.0.0.2"},
    )
    assert admin_auth.client_ip(request, settings) == "unknown"


@pytest.mark.integration
def test_proxy_middleware_rotating_xff_without_cf_shares_unknown_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", TRUSTED_CIDRS)
    settings = get_settings()
    for index in range(3):
        request = _request(
            "10.0.0.2",
            {"X-Forwarded-For": f"203.0.113.{index}, 10.0.0.2"},
        )
        assert admin_auth.client_ip(request, settings) == "unknown"

    assert (
        admin_auth.build_source_rate_limit_key("unknown")
        == admin_auth.build_source_rate_limit_key("unknown")
    )


@pytest.mark.integration
def test_proxy_middleware_trusted_chain_with_cf_resolves_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", TRUSTED_CIDRS)
    settings = get_settings()
    request = _request(
        "10.0.0.2",
        {
            "X-Forwarded-For": "203.0.113.99, 203.0.113.10, 10.0.0.2",
            "CF-Connecting-IP": "203.0.113.10",
        },
    )
    assert admin_auth.client_ip(request, settings) == "203.0.113.10"
