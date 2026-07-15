"""Deployment configuration consistency for admin proxy trust (#239)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.admin_client_source import (
    PRODUCTION_TRUSTED_PROXY_CIDRS,
    RENDER_FORWARDED_ALLOW_IPS,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
RENDER_YAML = REPO_ROOT / "render.yaml"
ADMIN_AUTH_DOC = REPO_ROOT / "docs" / "ADMIN_AUTH.md"


def _render_env_value(key: str) -> str | None:
    text = RENDER_YAML.read_text(encoding="utf-8")
    match = re.search(
        rf"- key: {re.escape(key)}\n\s+value: \"([^\"]+)\"",
        text,
    )
    return match.group(1) if match else None


@pytest.mark.unit
def test_render_yaml_declares_proxy_trust_env_and_uvicorn_flag() -> None:
    text = RENDER_YAML.read_text(encoding="utf-8")
    assert "--forwarded-allow-ips" in text
    assert "$UVICORN_FORWARDED_ALLOW_IPS" in text
    assert _render_env_value("ADMIN_TRUST_PROXY_HEADERS") == "true"
    assert _render_env_value("UVICORN_FORWARDED_ALLOW_IPS") == RENDER_FORWARDED_ALLOW_IPS
    assert _render_env_value("ADMIN_TRUSTED_PROXY_CIDRS") == PRODUCTION_TRUSTED_PROXY_CIDRS


@pytest.mark.unit
def test_admin_auth_doc_documents_proxy_trust_model() -> None:
    doc = ADMIN_AUTH_DOC.read_text(encoding="utf-8")
    assert "Cloudflare edge → Render load balancer → Uvicorn" in doc
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in doc
    assert "UVICORN_FORWARDED_ALLOW_IPS" in doc
    assert "right to left" in doc.lower() or "right-to-left" in doc.lower()
    assert "left-most" not in doc.lower()
    assert "leftmost" not in doc.lower()


@pytest.mark.unit
def test_render_forwarded_allow_ips_is_render_internal_only() -> None:
    assert "173.245.48.0/20" not in RENDER_FORWARDED_ALLOW_IPS
    assert "10.0.0.0/8" in RENDER_FORWARDED_ALLOW_IPS


@pytest.mark.unit
def test_production_trusted_cidrs_include_cloudflare_and_render() -> None:
    assert "10.0.0.0/8" in PRODUCTION_TRUSTED_PROXY_CIDRS
    assert "173.245.48.0/20" in PRODUCTION_TRUSTED_PROXY_CIDRS
