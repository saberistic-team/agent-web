"""Deployment configuration consistency for admin proxy trust (#239)."""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
RENDER_YAML = REPO_ROOT / "render.yaml"
ADMIN_AUTH_DOC = REPO_ROOT / "docs" / "ADMIN_AUTH.md"

EXPECTED_FORWARDED_ALLOW_IPS = (
    "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.1/32,::1/128"
)
EXPECTED_TRUSTED_PROXY_CIDRS = EXPECTED_FORWARDED_ALLOW_IPS


@pytest.mark.unit
def test_render_start_command_configures_uvicorn_forwarded_allow_ips() -> None:
    text = RENDER_YAML.read_text(encoding="utf-8")
    assert "forwarded-allow-ips" in text
    assert EXPECTED_FORWARDED_ALLOW_IPS in text


@pytest.mark.unit
def test_render_env_declares_admin_trusted_proxy_cidrs() -> None:
    text = RENDER_YAML.read_text(encoding="utf-8")
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in text
    assert EXPECTED_TRUSTED_PROXY_CIDRS in text


@pytest.mark.unit
def test_render_declares_cloudflare_cidr_env_slot() -> None:
    text = RENDER_YAML.read_text(encoding="utf-8")
    assert "ADMIN_CLOUDFLARE_PROXY_CIDRS" in text


@pytest.mark.unit
def test_admin_auth_doc_matches_render_trust_model() -> None:
    doc = ADMIN_AUTH_DOC.read_text(encoding="utf-8")
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in doc
    assert "right-to-left" in doc.lower() or "right-to-left" in doc
    assert "forwarded-allow-ips" in doc
    assert "ADMIN_TRUST_PROXY_HEADERS" in doc
    assert EXPECTED_TRUSTED_PROXY_CIDRS.split(",")[0] in doc or "10.0.0.0/8" in doc
