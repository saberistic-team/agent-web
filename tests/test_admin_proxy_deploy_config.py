"""Deployment configuration consistency for admin login proxy trust."""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
RENDER_TRUSTED = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.1,::1"


@pytest.mark.unit
def test_render_yaml_declares_proxy_trust_settings() -> None:
    render = (REPO_ROOT / "render.yaml").read_text()
    assert "startCommand: uvicorn app.main:app" in render
    assert "--forwarded-allow-ips" in render
    assert 'ADMIN_TRUST_PROXY_HEADERS\n        value: "true"' in render
    assert f"ADMIN_TRUSTED_PROXY_CIDRS\n        value: \"{RENDER_TRUSTED}\"" in render
    assert f"FORWARDED_ALLOW_IPS\n        value: \"{RENDER_TRUSTED}\"" in render
    assert RENDER_TRUSTED in render


@pytest.mark.unit
def test_admin_auth_docs_document_proxy_trust_model() -> None:
    docs = (REPO_ROOT / "docs" / "ADMIN_AUTH.md").read_text()
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in docs
    assert "FORWARDED_ALLOW_IPS" in docs
    assert "right-to-left" in docs
    assert "admin_login_source_trust" in docs
