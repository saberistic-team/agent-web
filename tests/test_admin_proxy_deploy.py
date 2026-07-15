"""Deployment configuration consistency for admin proxy trust (#239)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.admin_client_source import CLOUDFLARE_EDGE_CIDRS, RENDER_PLATFORM_CIDRS, resolve_trusted_proxy_cidr_strings
from app.config import get_settings
from app.main import app

REPO_ROOT = Path(__file__).resolve().parent.parent
RENDER_YAML = REPO_ROOT / "render.yaml"


@pytest.mark.unit
def test_render_yaml_declares_proxy_trust_preset() -> None:
    text = RENDER_YAML.read_text(encoding="utf-8")
    assert "ADMIN_TRUSTED_PROXY_PRESET" in text
    assert "cloudflare-render" in text
    assert "ADMIN_TRUST_PROXY_HEADERS" not in text


@pytest.mark.unit
def test_render_start_command_configures_uvicorn_forwarded_allow_ips() -> None:
    text = RENDER_YAML.read_text(encoding="utf-8")
    assert "startCommand:" in text
    assert "--proxy-headers" in text
    assert "--forwarded-allow-ips" in text
    assert "127.0.0.1" in text
    assert "10.0.0.0/8" in text


@pytest.mark.unit
def test_cloudflare_render_preset_matches_documented_platform_cidrs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_PRESET", "cloudflare-render")
    cidrs = resolve_trusted_proxy_cidr_strings(get_settings())
    for required in RENDER_PLATFORM_CIDRS:
        assert required in cidrs
    for required in CLOUDFLARE_EDGE_CIDRS:
        assert required in cidrs


@pytest.mark.unit
def test_health_reports_proxy_trust_label_when_preset_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_PRESET", "cloudflare-render")
    client = TestClient(app)
    payload = client.get("/health").json()
    assert payload["admin_proxy_trust"] == "cloudflare-render"
