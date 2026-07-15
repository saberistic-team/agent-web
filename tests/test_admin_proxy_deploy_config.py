"""Deployment configuration tests for admin login proxy trust."""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
RENDER_YAML = REPO_ROOT / "render.yaml"
ADMIN_AUTH_DOC = REPO_ROOT / "docs" / "ADMIN_AUTH.md"

EXPECTED_FORWARDED_ALLOW_IPS = (
    "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,100.64.0.0/10"
)


@pytest.mark.unit
def test_render_yaml_declares_proxy_trust_env_and_uvicorn_boundary() -> None:
    text = RENDER_YAML.read_text(encoding="utf-8")
    assert "ADMIN_TRUST_PROXY_HEADERS" in text
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in text
    assert EXPECTED_FORWARDED_ALLOW_IPS in text
    assert "--forwarded-allow-ips" in text
    assert "uvicorn app.main:app" in text


@pytest.mark.unit
def test_admin_auth_doc_matches_render_proxy_trust_model() -> None:
    text = ADMIN_AUTH_DOC.read_text(encoding="utf-8")
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in text
    assert "CF-Connecting-IP" in text
    assert "right-to-left" in text
    assert "forwarded-allow-ips" in text
    assert "admin_proxy_trust" in text
