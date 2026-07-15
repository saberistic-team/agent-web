"""Deployment configuration consistency for admin login proxy trust (#239)."""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
RENDER_YAML = REPO_ROOT / "render.yaml"
ADMIN_AUTH_DOC = REPO_ROOT / "docs" / "ADMIN_AUTH.md"

EXPECTED_FORWARDED_ALLOW_IPS = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
EXPECTED_TRUSTED_PROXY_CIDRS = EXPECTED_FORWARDED_ALLOW_IPS


@pytest.mark.unit
def test_render_start_command_disables_unsafe_uvicorn_proxy_rewrite() -> None:
    text = RENDER_YAML.read_text(encoding="utf-8")
    assert "--no-proxy-headers" in text
    assert f"--forwarded-allow-ips={EXPECTED_FORWARDED_ALLOW_IPS}" in text


@pytest.mark.unit
def test_render_declares_admin_proxy_trust_env_vars() -> None:
    text = RENDER_YAML.read_text(encoding="utf-8")
    assert "ADMIN_TRUST_PROXY_HEADERS" in text
    assert 'value: "true"' in text or "value: 'true'" in text
    assert f"ADMIN_TRUSTED_PROXY_CIDRS" in text
    assert EXPECTED_TRUSTED_PROXY_CIDRS in text
    assert "ADMIN_CLOUDFLARE_PROXY_CIDRS" in text
    assert "173.245.48.0/20" in text
    assert "104.16.0.0/13" in text


@pytest.mark.unit
def test_admin_auth_doc_matches_render_proxy_trust_model() -> None:
    text = ADMIN_AUTH_DOC.read_text(encoding="utf-8")
    assert "--no-proxy-headers" in text
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in text
    assert "ADMIN_CLOUDFLARE_PROXY_CIDRS" in text
    assert "CF-Connecting-IP" in text
    assert "right-to-left" in text.lower()
