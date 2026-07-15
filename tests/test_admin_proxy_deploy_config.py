"""Deployment configuration consistency for admin proxy trust (#239)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.client_source import DEFAULT_CLOUDFLARE_EDGE_CIDRS, RENDER_TRUSTED_PROXY_CIDRS

REPO_ROOT = Path(__file__).resolve().parent.parent
RENDER_YAML = REPO_ROOT / "render.yaml"
ADMIN_AUTH_DOC = REPO_ROOT / "docs" / "ADMIN_AUTH.md"


@pytest.mark.unit
def test_render_start_command_uses_asgi_entrypoint_and_proxy_flags() -> None:
    content = RENDER_YAML.read_text(encoding="utf-8")
    assert "uvicorn app.asgi:app" in content
    assert "--proxy-headers" in content
    assert "--forwarded-allow-ips=" in content


@pytest.mark.unit
def test_render_proxy_env_matches_trusted_proxy_cidrs() -> None:
    content = RENDER_YAML.read_text(encoding="utf-8")
    expected = ",".join(RENDER_TRUSTED_PROXY_CIDRS)
    assert "ADMIN_TRUST_PROXY_HEADERS" in content
    assert f'ADMIN_TRUSTED_PROXY_IPS\n        value: "{expected}"' in content
    assert f'ADMIN_FORWARDED_ALLOW_IPS\n        value: "{expected}"' in content


@pytest.mark.unit
def test_render_declares_cloudflare_edge_cidrs() -> None:
    content = RENDER_YAML.read_text(encoding="utf-8")
    expected = ",".join(DEFAULT_CLOUDFLARE_EDGE_CIDRS)
    assert f'ADMIN_CLOUDFLARE_EDGE_IPS\n        value: "{expected}"' in content


@pytest.mark.unit
def test_admin_auth_doc_documents_trusted_proxy_model() -> None:
    content = ADMIN_AUTH_DOC.read_text(encoding="utf-8")
    assert "ADMIN_TRUSTED_PROXY_IPS" in content
    assert "ADMIN_CLOUDFLARE_EDGE_IPS" in content
    assert "ADMIN_FORWARDED_ALLOW_IPS" in content
    assert "right-to-left" in content.lower() or "right to left" in content.lower()
    assert "CF-Connecting-IP" in content
    assert "rollback" in content.lower()
