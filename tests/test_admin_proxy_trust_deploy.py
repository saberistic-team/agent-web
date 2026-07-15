"""Deployment configuration checks for admin proxy trust (#239)."""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
RENDER_YAML = REPO_ROOT / "render.yaml"
ADMIN_AUTH_DOC = REPO_ROOT / "docs" / "ADMIN_AUTH.md"


@pytest.mark.unit
def test_render_yaml_declares_explicit_uvicorn_forwarded_allow_ips() -> None:
    content = RENDER_YAML.read_text(encoding="utf-8")
    assert "startCommand:" in content
    assert "--forwarded-allow-ips=''" in content or '--forwarded-allow-ips=""' in content


@pytest.mark.unit
def test_render_yaml_enables_admin_proxy_trust_settings() -> None:
    content = RENDER_YAML.read_text(encoding="utf-8")
    assert "ADMIN_TRUST_PROXY_HEADERS" in content
    assert 'value: "true"' in content
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in content


@pytest.mark.unit
def test_admin_auth_doc_matches_runtime_trust_model() -> None:
    content = ADMIN_AUTH_DOC.read_text(encoding="utf-8")
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in content
    assert "--forwarded-allow-ips=''" in content
    assert "CF-Connecting-IP" in content
    assert "right-to-left" in content
    assert "Cloudflare" in content
