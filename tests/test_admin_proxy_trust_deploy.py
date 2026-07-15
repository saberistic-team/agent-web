"""Deployment configuration checks for admin login proxy trust (#239)."""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
RENDER_YAML = REPO_ROOT / "render.yaml"


@pytest.mark.unit
def test_render_yaml_declares_uvicorn_forwarded_allow_ips() -> None:
    text = RENDER_YAML.read_text(encoding="utf-8")
    assert "--proxy-headers" in text
    assert "--forwarded-allow-ips=${UVICORN_FORWARDED_ALLOW_IPS}" in text
    assert "UVICORN_FORWARDED_ALLOW_IPS" in text
    assert "10.0.0.0/8" in text


@pytest.mark.unit
def test_render_yaml_proxy_trust_env_matches_uvicorn_boundary() -> None:
    text = RENDER_YAML.read_text(encoding="utf-8")
    render_trusted = 'value: "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.1"'
    assert text.count(render_trusted) >= 2
    assert "ADMIN_CLOUDFLARE_EDGE_CIDRS" in text
    assert "173.245.48.0/20" in text


@pytest.mark.unit
def test_admin_auth_docs_describe_proxy_trust_model() -> None:
    text = (REPO_ROOT / "docs" / "ADMIN_AUTH.md").read_text(encoding="utf-8")
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in text
    assert "UVICORN_FORWARDED_ALLOW_IPS" in text
    assert "ADMIN_CLOUDFLARE_EDGE_CIDRS" in text
    assert "Cloudflare" in text
    assert "right-to-left" in text.lower() or "right to left" in text.lower()
