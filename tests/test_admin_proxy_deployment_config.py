"""Deployment configuration consistency for admin proxy trust (#239)."""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
RENDER_YAML = REPO_ROOT / "render.yaml"
ADMIN_AUTH_DOC = REPO_ROOT / "docs" / "ADMIN_AUTH.md"

PRODUCTION_TRUSTED_CIDRS = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.1/32"
PRODUCTION_FORWARDED_ALLOW_IPS = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.1"


def _render_yaml_text() -> str:
    return RENDER_YAML.read_text(encoding="utf-8")


@pytest.mark.unit
def test_render_yaml_declares_proxy_trust_settings() -> None:
    text = _render_yaml_text()
    assert "ADMIN_TRUST_PROXY_HEADERS" in text
    assert 'value: "true"' in text or "value: 'true'" in text
    assert f"value: \"{PRODUCTION_TRUSTED_CIDRS}\"" in text
    assert f"value: \"{PRODUCTION_FORWARDED_ALLOW_IPS}\"" in text
    assert "--forwarded-allow-ips=" in text
    assert PRODUCTION_FORWARDED_ALLOW_IPS in text


@pytest.mark.unit
def test_admin_auth_doc_documents_same_trust_model() -> None:
    text = ADMIN_AUTH_DOC.read_text(encoding="utf-8")
    assert "ADMIN_TRUSTED_PROXY_IPS" in text
    assert "UVICORN_FORWARDED_ALLOW_IPS" in text
    assert "right-to-left" in text
    assert "CF-Connecting-IP" in text
    assert "Cloudflare" in text
    assert "render.yaml" in text


@pytest.mark.unit
def test_render_forwarded_allow_ips_match_env_var() -> None:
    text = _render_yaml_text()
    assert "key: ADMIN_TRUSTED_PROXY_IPS" in text
    assert "key: UVICORN_FORWARDED_ALLOW_IPS" in text
    assert PRODUCTION_FORWARDED_ALLOW_IPS in text
