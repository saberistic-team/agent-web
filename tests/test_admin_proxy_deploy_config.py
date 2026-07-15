"""Deployment configuration tests for admin proxy-trust settings."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.verify_admin_proxy_config import verify_admin_proxy_config

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.unit
def test_render_yaml_declares_explicit_proxy_trust_settings() -> None:
    render_text = (REPO_ROOT / "render.yaml").read_text(encoding="utf-8")
    assert "--no-proxy-headers" in render_text
    assert "ADMIN_TRUST_PROXY_HEADERS" in render_text
    assert "ADMIN_TRUSTED_PROXY_IPS" in render_text
    assert 'value: "true"' in render_text
    assert "ADMIN_TRUST_CLOUDFLARE_HEADERS" in render_text
    assert "ADMIN_CLOUDFLARE_PROXY_CIDRS" in render_text
    assert "--proxy-headers" not in render_text.replace("--no-proxy-headers", "")


@pytest.mark.unit
def test_admin_auth_doc_matches_runtime_trust_model() -> None:
    doc_text = (REPO_ROOT / "docs" / "ADMIN_AUTH.md").read_text(encoding="utf-8")
    assert "resolve_admin_login_client_source" in doc_text
    assert "ADMIN_TRUSTED_PROXY_IPS" in doc_text
    assert "--no-proxy-headers" in doc_text
    assert "right to left" in doc_text.lower() or "right-to-left" in doc_text.lower()


@pytest.mark.unit
def test_verify_admin_proxy_config_script_passes() -> None:
    errors = verify_admin_proxy_config(
        render_path=REPO_ROOT / "render.yaml",
        docs_path=REPO_ROOT / "docs" / "ADMIN_AUTH.md",
    )
    assert errors == []
