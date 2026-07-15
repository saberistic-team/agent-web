"""Deployment configuration tests for admin proxy trust (#239)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
RENDER_YAML = REPO_ROOT / "render.yaml"
ADMIN_AUTH_DOC = REPO_ROOT / "docs" / "ADMIN_AUTH.md"


def _render_start_command() -> str:
    text = RENDER_YAML.read_text(encoding="utf-8")
    match = re.search(r"startCommand:\s*(.+)", text)
    assert match is not None
    return match.group(1).strip()


def _render_env_value(key: str) -> str:
    text = RENDER_YAML.read_text(encoding="utf-8")
    pattern = rf"- key: {re.escape(key)}\s+value:\s*(.+)"
    match = re.search(pattern, text)
    assert match is not None
    return match.group(1).strip().strip('"').strip("'")


@pytest.mark.unit
def test_render_start_command_disables_uvicorn_proxy_headers() -> None:
    start_command = _render_start_command()
    assert "--no-proxy-headers" in start_command
    assert "--forwarded-allow-ips=" in start_command


@pytest.mark.unit
def test_render_declares_trusted_proxy_cidrs() -> None:
    proxy_cidrs = _render_env_value("ADMIN_TRUSTED_PROXY_CIDRS")
    cloudflare_cidrs = _render_env_value("ADMIN_TRUSTED_CLOUDFLARE_CIDRS")
    assert "10.0.0.0/8" in proxy_cidrs
    assert "173.245.48.0/20" in cloudflare_cidrs


@pytest.mark.unit
def test_admin_auth_doc_matches_render_proxy_model() -> None:
    doc = ADMIN_AUTH_DOC.read_text(encoding="utf-8")
    start_command = _render_start_command()
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in doc
    assert "right-to-left" in doc.lower() or "right to left" in doc.lower()
    assert "--no-proxy-headers" in doc
    assert "--no-proxy-headers" in start_command
