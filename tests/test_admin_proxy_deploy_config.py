"""Deployment configuration consistency for admin proxy trust."""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.unit
def test_render_yaml_declares_admin_trusted_proxy_ips_and_uvicorn_flags() -> None:
    render_yaml = (REPO_ROOT / "render.yaml").read_text()
    assert "--proxy-headers" in render_yaml
    assert "--forwarded-allow-ips" in render_yaml
    assert "10.0.0.0/8" in render_yaml
    assert "ADMIN_TRUSTED_PROXY_IPS" in render_yaml
    assert "172.16.0.0/12" in render_yaml
    assert "192.168.0.0/16" in render_yaml


@pytest.mark.unit
def test_admin_auth_docs_describe_same_trust_model() -> None:
    doc = (REPO_ROOT / "docs" / "ADMIN_AUTH.md").read_text()
    assert "ADMIN_TRUSTED_PROXY_IPS" in doc
    assert "--forwarded-allow-ips" in doc
    assert "right-to-left" in doc.lower()
    assert "admin_source_trust" in doc


@pytest.mark.unit
def test_smoke_deploy_verifies_admin_source_trust_field() -> None:
    script = (REPO_ROOT / "scripts" / "smoke_deploy.py").read_text()
    assert "admin_source_trust" in script
    assert "trusted_proxy_boundary" in script
