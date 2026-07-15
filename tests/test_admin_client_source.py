"""Tests for trusted-proxy admin login client source resolution (#239)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.client_source import (
    RENDER_FORWARDED_ALLOW_IPS,
    ClientSourcePath,
    normalize_client_address,
    resolve_admin_login_client_source,
)
from app.config import Settings, get_settings
from app.main import app as main_app

TRUSTED_RENDER_PROXY = "10.0.0.1"
TRUSTED_PROXY_IPS = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.1,::1,testclient"


def _settings(**overrides: Any) -> Settings:
    base = get_settings()
    return Settings(
        **{
            **base.__dict__,
            "admin_trusted_proxy_ips": tuple(),
            "admin_trust_proxy_headers": False,
            **overrides,
        }
    )


def _request(
    *,
    peer: str | None = "198.51.100.10",
    headers: list[tuple[bytes, bytes]] | None = None,
) -> Request:
    scope: dict[str, Any] = {
        "type": "http",
        "headers": headers or [],
        "client": (peer, 12345) if peer is not None else None,
        "method": "POST",
        "path": "/admin/login",
    }
    return Request(scope)


def _resolve(
    request: Request,
    *,
    trusted_proxy_ips: str = "",
    trust_proxy_headers: bool = False,
) -> str:
    settings = _settings(
        admin_trusted_proxy_ips=tuple(
            entry.strip()
            for entry in trusted_proxy_ips.split(",")
            if entry.strip()
        ),
        admin_trust_proxy_headers=trust_proxy_headers,
    )
    return resolve_admin_login_client_source(request, settings).address


@pytest.mark.unit
def test_normalize_client_address_formats() -> None:
    assert normalize_client_address("203.0.113.1") == "203.0.113.1"
    assert normalize_client_address("2001:0db8::1") == "2001:db8::1"
    assert normalize_client_address("::ffff:203.0.113.1") == "203.0.113.1"
    assert normalize_client_address("203.0.113.1:8080") == "203.0.113.1"
    assert normalize_client_address("[2001:db8::1]:443") == "2001:db8::1"
    assert normalize_client_address("  ") is None
    assert normalize_client_address("not-an-ip") is None


@pytest.mark.unit
def test_direct_spoof_single_and_multi_value_xff_ignored() -> None:
    settings = _settings()
    for header_value in (
        b"203.0.113.99",
        b"203.0.113.50, 10.0.0.1, 198.51.100.10",
    ):
        request = _request(
            peer="198.51.100.10",
            headers=[(b"x-forwarded-for", header_value)],
        )
        resolution = resolve_admin_login_client_source(request, settings)
        assert resolution.address == "198.51.100.10"
        assert resolution.path == ClientSourcePath.UNTRUSTED_FORWARDING_IGNORED


@pytest.mark.unit
def test_cloudflare_append_ignores_attacker_leftmost() -> None:
    request = _request(
        peer=TRUSTED_RENDER_PROXY,
        headers=[
            (
                b"x-forwarded-for",
                b"203.0.113.99, 198.51.100.55",
            )
        ],
    )
    assert (
        _resolve(
            request,
            trusted_proxy_ips=TRUSTED_PROXY_IPS,
        )
        == "198.51.100.55"
    )


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client() -> None:
    request = _request(
        peer=TRUSTED_RENDER_PROXY,
        headers=[(b"x-forwarded-for", b"203.0.113.50, 10.0.0.1")],
    )
    assert (
        _resolve(
            request,
            trusted_proxy_ips=TRUSTED_PROXY_IPS,
        )
        == "203.0.113.50"
    )


@pytest.mark.unit
def test_partial_trust_untrusted_intermediary_fails_closed() -> None:
    request = _request(
        peer="203.0.113.200",
        headers=[(b"x-forwarded-for", b"203.0.113.50, 10.0.0.1")],
    )
    resolution = resolve_admin_login_client_source(
        request,
        _settings(admin_trusted_proxy_ips=("10.0.0.0/8",)),
    )
    assert resolution.address == "203.0.113.200"
    assert resolution.path == ClientSourcePath.UNTRUSTED_FORWARDING_IGNORED


@pytest.mark.unit
def test_direct_render_origin_ignores_cf_connecting_ip() -> None:
    request = _request(
        peer="198.51.100.10",
        headers=[(b"cf-connecting-ip", b"203.0.113.77")],
    )
    resolution = resolve_admin_login_client_source(
        request,
        _settings(admin_trusted_proxy_ips=("10.0.0.0/8",)),
    )
    assert resolution.address == "198.51.100.10"
    assert resolution.path == ClientSourcePath.UNTRUSTED_FORWARDING_IGNORED


@pytest.mark.unit
def test_header_precedence_xff_over_cf_connecting_ip() -> None:
    request = _request(
        peer=TRUSTED_RENDER_PROXY,
        headers=[
            (b"x-forwarded-for", b"203.0.113.10"),
            (b"cf-connecting-ip", b"203.0.113.99"),
        ],
    )
    assert (
        _resolve(
            request,
            trusted_proxy_ips=TRUSTED_PROXY_IPS,
        )
        == "203.0.113.10"
    )


@pytest.mark.unit
def test_forwarded_header_used_when_xff_missing() -> None:
    request = _request(
        peer=TRUSTED_RENDER_PROXY,
        headers=[(b"forwarded", b'for="203.0.113.44";proto=https, for=10.0.0.1')],
    )
    resolution = resolve_admin_login_client_source(
        request,
        _settings(admin_trusted_proxy_ips=("10.0.0.0/8",)),
    )
    assert resolution.address == "203.0.113.44"
    assert resolution.path == ClientSourcePath.TRUSTED_FORWARDED


@pytest.mark.unit
def test_cf_connecting_ip_only_when_peer_trusted_and_xff_empty() -> None:
    request = _request(
        peer=TRUSTED_RENDER_PROXY,
        headers=[(b"cf-connecting-ip", b"203.0.113.88")],
    )
    resolution = resolve_admin_login_client_source(
        request,
        _settings(admin_trusted_proxy_ips=("10.0.0.0/8",)),
    )
    assert resolution.address == "203.0.113.88"
    assert resolution.path == ClientSourcePath.TRUSTED_CF_CONNECTING_IP


@pytest.mark.unit
def test_overlong_forwarding_chain_falls_back_conservatively() -> None:
    chain = ", ".join(f"10.0.0.{index}" for index in range(1, 40))
    request = _request(
        peer=TRUSTED_RENDER_PROXY,
        headers=[(b"x-forwarded-for", chain.encode())],
    )
    resolution = resolve_admin_login_client_source(
        request,
        _settings(admin_trusted_proxy_ips=("10.0.0.0/8",)),
    )
    assert resolution.address == TRUSTED_RENDER_PROXY
    assert resolution.path == ClientSourcePath.MALFORMED_FORWARDING


@pytest.mark.unit
def test_legacy_trust_flag_without_ips_fails_closed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    request = _request(
        peer="198.51.100.10",
        headers=[(b"x-forwarded-for", b"203.0.113.99")],
    )
    with caplog.at_level(logging.WARNING):
        resolution = resolve_admin_login_client_source(
            request,
            _settings(admin_trust_proxy_headers=True),
        )
    assert resolution.address == "198.51.100.10"
    assert "ADMIN_TRUSTED_PROXY_IPS" in caplog.text


@pytest.mark.unit
def test_missing_peer_returns_unknown() -> None:
    request = _request(peer=None)
    resolution = resolve_admin_login_client_source(request, _settings())
    assert resolution.address == "unknown"
    assert resolution.path == ClientSourcePath.MISSING_PEER


@pytest.mark.unit
def test_telemetry_contains_no_raw_addresses(caplog: pytest.LogCaptureFixture) -> None:
    request = _request(
        peer=TRUSTED_RENDER_PROXY,
        headers=[(b"x-forwarded-for", b"203.0.113.50, 10.0.0.1")],
    )
    with caplog.at_level(logging.DEBUG):
        resolve_admin_login_client_source(
            request,
            _settings(admin_trusted_proxy_ips=("10.0.0.0/8",)),
        )
    combined = caplog.text
    assert "203.0.113.50" not in combined
    assert "admin_client_source_path" in combined or "Admin login client source resolved" in combined


@pytest.mark.unit
def test_render_yaml_proxy_settings_are_consistent() -> None:
    from pathlib import Path

    render_yaml = Path("render.yaml").read_text(encoding="utf-8")
    assert "--forwarded-allow-ips='10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.1,::1'" in render_yaml
    assert "ADMIN_TRUSTED_PROXY_IPS" in render_yaml
    assert RENDER_FORWARDED_ALLOW_IPS.replace("'", "") in render_yaml.replace("'", "")


@pytest.mark.unit
def test_smoke_deploy_checks_admin_proxy_trust(monkeypatch: pytest.MonkeyPatch) -> None:
    from smoke_deploy import main

    calls: list[str] = []

    def fake_get_json(url: str) -> dict:
        calls.append(url)
        if url.endswith("/health"):
            return {"status": "ok", "admin_proxy_trust": {"configured": True}}
        if url.endswith("/hello"):
            return {"message": "hello world"}
        raise AssertionError(url)

    monkeypatch.setattr("smoke_deploy.get_json", fake_get_json)
    assert main(["--base-url", "https://example.com"]) == 0
    assert any(url.endswith("/health") for url in calls)


@pytest.mark.unit
@pytest.mark.integration
def test_admin_login_source_through_uvicorn_proxy_middleware(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe_app = FastAPI()

    @probe_app.post("/probe")
    async def probe(request: Request) -> dict[str, str]:
        settings = get_settings()
        resolution = resolve_admin_login_client_source(request, settings)
        return {
            "address": resolution.address,
            "path": resolution.path.value,
        }

    wrapped = ProxyHeadersMiddleware(probe_app, trusted_hosts=RENDER_FORWARDED_ALLOW_IPS)
    monkeypatch.setenv(
        "ADMIN_TRUSTED_PROXY_IPS",
        RENDER_FORWARDED_ALLOW_IPS,
    )

    async def _request() -> httpx.Response:
        transport = httpx.ASGITransport(app=wrapped)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http_client:
            return await http_client.post(
                "/probe",
                headers={
                    "X-Forwarded-For": "203.0.113.99, 198.51.100.77, 10.0.0.1",
                },
            )

    response = asyncio.run(_request())

    assert response.status_code == 200
    payload = response.json()
    assert payload["address"] == "198.51.100.77"
    assert payload["path"] in {
        ClientSourcePath.TRUSTED_CHAIN.value,
        ClientSourcePath.UNTRUSTED_FORWARDING_IGNORED.value,
    }


@pytest.mark.unit
def test_main_app_health_reports_proxy_trust(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", RENDER_FORWARDED_ALLOW_IPS)
    health_client = TestClient(main_app)
    response = health_client.get("/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["admin_proxy_trust"] == {"configured": True}
