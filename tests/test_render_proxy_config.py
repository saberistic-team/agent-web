"""Deployment configuration consistency for trusted admin login proxy trust (#239)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.admin_client_source import DEFAULT_RENDER_TRUSTED_PROXY_CIDRS

REPO_ROOT = Path(__file__).resolve().parents[1]
RENDER_YAML = REPO_ROOT / "render.yaml"
ADMIN_AUTH_DOC = REPO_ROOT / "docs" / "ADMIN_AUTH.md"

EXPECTED_PROXY_CIDRS = ",".join(DEFAULT_RENDER_TRUSTED_PROXY_CIDRS)


@pytest.mark.unit
def test_render_yaml_declares_matching_proxy_trust_settings() -> None:
    text = RENDER_YAML.read_text(encoding="utf-8")
    assert "--forwarded-allow-ips" in text
    assert EXPECTED_PROXY_CIDRS in text
    assert f"ADMIN_TRUSTED_PROXY_CIDRS\n        value: \"{EXPECTED_PROXY_CIDRS}\"" in text
    assert f"UVICORN_FORWARDED_ALLOW_IPS\n        value: \"{EXPECTED_PROXY_CIDRS}\"" in text


@pytest.mark.unit
def test_admin_auth_doc_documents_same_trust_boundary() -> None:
    text = ADMIN_AUTH_DOC.read_text(encoding="utf-8")
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in text
    assert "UVICORN_FORWARDED_ALLOW_IPS" in text
    assert "--forwarded-allow-ips" in text
    assert "Cloudflare" in text
    assert "Render load balancer" in text
    assert "Uvicorn" in text
    assert "right-to-left" in text
    assert "10/8" in text
    assert "172.16/12" in text
    assert "192.168/16" in text
