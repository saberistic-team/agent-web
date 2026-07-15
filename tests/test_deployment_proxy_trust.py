"""Deployment configuration tests for admin proxy trust settings."""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
RENDER_YAML = REPO_ROOT / "render.yaml"
ADMIN_AUTH_DOC = REPO_ROOT / "docs" / "ADMIN_AUTH.md"


@pytest.mark.unit
def test_render_yaml_declares_uvicorn_forwarded_allow_ips() -> None:
    text = RENDER_YAML.read_text(encoding="utf-8")
    assert "--proxy-headers" in text
    assert "--forwarded-allow-ips" in text
    assert "10.0.0.0/8" in text


@pytest.mark.unit
def test_render_yaml_declares_admin_trusted_proxy_env_vars() -> None:
    text = RENDER_YAML.read_text(encoding="utf-8")
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in text
    assert "ADMIN_TRUSTED_EDGE_CIDRS" in text
    assert "172.64.0.0/13" in text


@pytest.mark.unit
def test_admin_auth_doc_matches_render_proxy_trust_model() -> None:
    text = ADMIN_AUTH_DOC.read_text(encoding="utf-8")
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in text
    assert "ADMIN_TRUSTED_EDGE_CIDRS" in text
    assert "--forwarded-allow-ips" in text
    assert "ADMIN_TRUST_PROXY_HEADERS" in text
    assert "left-most" not in text
