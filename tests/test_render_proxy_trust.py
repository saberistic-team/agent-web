"""Deployment configuration tests for admin proxy trust (#239)."""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
RENDER_YAML = REPO_ROOT / "render.yaml"
ADMIN_AUTH_DOC = REPO_ROOT / "docs" / "ADMIN_AUTH.md"

PRODUCTION_TRUSTED_CIDRS = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "100.64.0.0/10",
    "fd00::/8",
)


@pytest.mark.unit
def test_render_yaml_declares_forwarded_allow_ips_and_matching_env() -> None:
    content = RENDER_YAML.read_text(encoding="utf-8")
    assert "--forwarded-allow-ips" in content
    for cidr in PRODUCTION_TRUSTED_CIDRS:
        assert cidr in content
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in content


@pytest.mark.unit
def test_render_start_command_and_env_cidrs_are_consistent() -> None:
    lines = RENDER_YAML.read_text(encoding="utf-8").splitlines()
    start_line = next(line for line in lines if "startCommand:" in line)
    env_index = next(index for index, line in enumerate(lines) if "ADMIN_TRUSTED_PROXY_CIDRS" in line)
    env_value = lines[env_index + 1].split("value:", 1)[1].strip()
    allow_ips = start_line.split("--forwarded-allow-ips", 1)[1].strip()
    assert allow_ips == env_value


@pytest.mark.unit
def test_admin_auth_doc_documents_trust_model() -> None:
    content = ADMIN_AUTH_DOC.read_text(encoding="utf-8")
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in content
    assert "Cloudflare" in content
    assert "forwarded-allow-ips" in content
    assert "ADMIN_TRUST_PROXY_HEADERS" not in content
    assert "Rollback" in content or "rollback" in content.lower()
