"""Deployment configuration consistency for admin proxy trust."""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
RENDER_YAML = REPO_ROOT / "render.yaml"
ADMIN_AUTH_DOC = REPO_ROOT / "docs" / "ADMIN_AUTH.md"

EXPECTED_FORWARDED_ALLOW_IPS = (
    "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.1/32"
)


@pytest.mark.unit
def test_render_yaml_declares_proxy_trust_settings() -> None:
    text = RENDER_YAML.read_text(encoding="utf-8")
    assert "--forwarded-allow-ips='10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.1/32'" in text
    assert "ADMIN_TRUST_PROXY_HEADERS" in text
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in text
    assert "ADMIN_CLOUDFLARE_PROXY_CIDRS" in text


@pytest.mark.unit
def test_render_yaml_proxy_settings_are_internally_consistent() -> None:
    text = RENDER_YAML.read_text(encoding="utf-8")
    assert EXPECTED_FORWARDED_ALLOW_IPS in text
    for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.1/32"):
        assert cidr in text


@pytest.mark.unit
def test_admin_auth_doc_matches_render_proxy_model() -> None:
    doc = ADMIN_AUTH_DOC.read_text(encoding="utf-8")
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in doc
    assert "ADMIN_CLOUDFLARE_PROXY_CIDRS" in doc
    assert "--forwarded-allow-ips" in doc
    assert "Client → Cloudflare → Render load balancer → Uvicorn" in doc
    assert "left-most raw ``X-Forwarded-For``" in doc
