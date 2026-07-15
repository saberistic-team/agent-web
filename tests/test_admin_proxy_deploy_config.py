"""Deployment configuration tests for admin proxy trust settings."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.proxy_trust_constants import (
    default_uvicorn_forwarded_allow_ips,
    production_trusted_proxy_cidrs,
)
from app.config import get_settings

REPO_ROOT = Path(__file__).resolve().parent.parent
RENDER_YAML = REPO_ROOT / "render.yaml"


def _render_env_value(key: str) -> str:
    text = RENDER_YAML.read_text(encoding="utf-8")
    match = re.search(
        rf"- key: {re.escape(key)}\n\s+value: \"([^\"]+)\"",
        text,
    )
    assert match is not None, f"missing {key} in render.yaml"
    return match.group(1)


@pytest.mark.unit
def test_render_yaml_declares_uvicorn_forwarded_allow_ips() -> None:
    text = RENDER_YAML.read_text(encoding="utf-8")
    assert "--forwarded-allow-ips" in text
    assert "${UVICORN_FORWARDED_ALLOW_IPS}" in text
    forwarded_allow_ips = _render_env_value("UVICORN_FORWARDED_ALLOW_IPS")
    trusted_proxy_cidrs = _render_env_value("ADMIN_TRUSTED_PROXY_CIDRS")
    assert "10.0.0.0/8" in forwarded_allow_ips
    assert "173.245.48.0/20" in trusted_proxy_cidrs


@pytest.mark.unit
def test_render_proxy_settings_match_application_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forwarded_allow_ips = _render_env_value("UVICORN_FORWARDED_ALLOW_IPS")
    trusted_proxy_cidrs = _render_env_value("ADMIN_TRUSTED_PROXY_CIDRS")
    monkeypatch.setenv("UVICORN_FORWARDED_ALLOW_IPS", forwarded_allow_ips)
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", trusted_proxy_cidrs)
    settings = get_settings()
    assert settings.uvicorn_forwarded_allow_ips == forwarded_allow_ips
    assert settings.admin_trusted_proxy_cidrs == tuple(
        cidr.strip() for cidr in trusted_proxy_cidrs.split(",") if cidr.strip()
    )


@pytest.mark.unit
def test_production_defaults_include_render_internal_and_cloudflare_ranges() -> None:
    trusted = production_trusted_proxy_cidrs()
    assert "10.0.0.0/8" in trusted
    assert "173.245.48.0/20" in trusted
    assert default_uvicorn_forwarded_allow_ips().startswith("10.0.0.0/8")
