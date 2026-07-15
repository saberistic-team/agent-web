"""Deployment configuration tests for admin login proxy trust."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.trusted_proxy_defaults import (
    PRODUCTION_CLOUDFLARE_PROXY_CIDRS,
    PRODUCTION_TRUSTED_PROXY_CIDRS,
    UVICORN_FORWARDED_ALLOW_IPS,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
RENDER_YAML = REPO_ROOT / "render.yaml"
ADMIN_AUTH_DOC = REPO_ROOT / "docs" / "ADMIN_AUTH.md"


def _render_env_value(key: str) -> str:
    text = RENDER_YAML.read_text(encoding="utf-8")
    match = re.search(
        rf"- key: {re.escape(key)}\n\s+value: \"([^\"]+)\"",
        text,
    )
    assert match is not None, f"missing render.yaml env value for {key}"
    return match.group(1)


def _render_start_command() -> str:
    text = RENDER_YAML.read_text(encoding="utf-8")
    match = re.search(r"startCommand: (.+)", text)
    assert match is not None
    return match.group(1).strip()


@pytest.mark.unit
def test_render_yaml_declares_proxy_trust_env_and_uvicorn_flags() -> None:
    start_command = _render_start_command()
    assert "--proxy-headers" in start_command
    assert "--forwarded-allow-ips=" in start_command
    assert _render_env_value("ADMIN_TRUST_PROXY_HEADERS") == "true"
    assert _render_env_value("ADMIN_TRUSTED_PROXY_CIDRS") == PRODUCTION_TRUSTED_PROXY_CIDRS
    assert _render_env_value("ADMIN_CLOUDFLARE_PROXY_CIDRS") == PRODUCTION_CLOUDFLARE_PROXY_CIDRS

    for render_cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"):
        assert render_cidr in start_command
        assert render_cidr in _render_env_value("ADMIN_TRUSTED_PROXY_CIDRS")


@pytest.mark.unit
def test_admin_auth_doc_matches_runtime_trust_model() -> None:
    body = ADMIN_AUTH_DOC.read_text(encoding="utf-8")
    assert "Cloudflare edge → Render load balancer → Uvicorn" in body
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in body
    assert "right-to-left" in body
    assert "--forwarded-allow-ips" in body
    assert "ADMIN_TRUST_PROXY_HEADERS=false" in body


@pytest.mark.unit
def test_uvicorn_forwarded_allow_ips_matches_render_start_command() -> None:
    start_command = _render_start_command()
    for cidr in UVICORN_FORWARDED_ALLOW_IPS.split(","):
        base = cidr.split("/")[0]
        assert base in start_command
