"""Deployment configuration tests for admin login proxy trust."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from scripts.verify_admin_proxy_config import (
    RENDER_TRUSTED_CIDRS,
    UVICORN_FORWARDED_ALLOW_IPS,
    verify_admin_proxy_config,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.unit
def test_render_yaml_proxy_settings_match_application_trust_model() -> None:
    render_text = (REPO_ROOT / "render.yaml").read_text(encoding="utf-8")
    start_match = re.search(r"startCommand:\s*(.+)", render_text)
    assert start_match is not None
    start_command = start_match.group(1)
    assert "--proxy-headers" in start_command
    assert f"--forwarded-allow-ips {UVICORN_FORWARDED_ALLOW_IPS}" in start_command

    trusted_match = re.search(
        r'- key: ADMIN_TRUSTED_PROXY_CIDRS\s+value: "([^"]+)"',
        render_text,
    )
    edge_match = re.search(
        r'- key: ADMIN_EDGE_PROXY_CIDRS\s+value: "([^"]+)"',
        render_text,
    )
    assert trusted_match is not None
    assert edge_match is not None
    assert trusted_match.group(1) == RENDER_TRUSTED_CIDRS
    assert "173.245.48.0/20" in edge_match.group(1)


@pytest.mark.unit
def test_verify_admin_proxy_config_script_passes() -> None:
    assert verify_admin_proxy_config() == []


@pytest.mark.unit
def test_admin_auth_doc_documents_proxy_chain_and_verification() -> None:
    doc_text = (REPO_ROOT / "docs" / "ADMIN_AUTH.md").read_text(encoding="utf-8")
    assert "Cloudflare (edge) → Render load balancer → Uvicorn" in doc_text
    assert "verify_admin_proxy_config.py" in doc_text
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in doc_text
    assert "ADMIN_TRUST_PROXY_HEADERS" not in doc_text
