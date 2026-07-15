"""Deployment configuration tests for admin proxy trust settings."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.proxy_trust_config import production_trusted_proxy_cidrs

REPO_ROOT = Path(__file__).resolve().parents[1]
RENDER_YAML = REPO_ROOT / "render.yaml"
ADMIN_AUTH_DOC = REPO_ROOT / "docs" / "ADMIN_AUTH.md"

EXPECTED_FORWARDED_ALLOW_IPS = production_trusted_proxy_cidrs()


@pytest.mark.unit
def test_render_start_command_configures_uvicorn_proxy_trust() -> None:
    content = RENDER_YAML.read_text(encoding="utf-8")
    assert "--proxy-headers" in content
    assert f"--forwarded-allow-ips {EXPECTED_FORWARDED_ALLOW_IPS}" in content


@pytest.mark.unit
def test_render_env_matches_forwarded_allow_ips() -> None:
    content = RENDER_YAML.read_text(encoding="utf-8")
    assert "ADMIN_TRUST_PROXY_HEADERS" in content
    assert f'value: "{EXPECTED_FORWARDED_ALLOW_IPS}"' in content


@pytest.mark.unit
def test_admin_auth_doc_documents_same_trust_boundary() -> None:
    content = ADMIN_AUTH_DOC.read_text(encoding="utf-8")
    assert "--forwarded-allow-ips" in content
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in content
    assert EXPECTED_FORWARDED_ALLOW_IPS in content
    assert "right-to-left" in content
