"""Deployment configuration consistency for admin client source trust (#239)."""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
RENDER_YAML = REPO_ROOT / "render.yaml"
ADMIN_AUTH_DOC = REPO_ROOT / "docs" / "ADMIN_AUTH.md"

PRODUCTION_TRUSTED_CIDRS = (
    "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.1/32"
)


@pytest.mark.unit
def test_render_yaml_disables_uvicorn_forwarded_header_trust() -> None:
    content = RENDER_YAML.read_text(encoding="utf-8")
    assert "--forwarded-allow-ips ''" in content


@pytest.mark.unit
def test_render_yaml_sets_admin_trusted_proxy_cidrs() -> None:
    content = RENDER_YAML.read_text(encoding="utf-8")
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in content
    assert PRODUCTION_TRUSTED_CIDRS in content.replace("\n", " ").replace('"', "")


@pytest.mark.unit
def test_admin_auth_doc_matches_render_proxy_model() -> None:
    content = ADMIN_AUTH_DOC.read_text(encoding="utf-8")
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in content
    assert "--forwarded-allow-ips" in content
    assert "CF-Connecting-IP" in content
    assert "right-to-left" in content
    assert "ADMIN_TRUST_PROXY_HEADERS" not in content


@pytest.mark.unit
def test_render_start_command_and_env_use_same_trust_boundary() -> None:
    content = RENDER_YAML.read_text(encoding="utf-8")
    assert content.index("--forwarded-allow-ips") < content.index("ADMIN_TRUSTED_PROXY_CIDRS")
