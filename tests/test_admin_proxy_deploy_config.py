"""Deployment configuration consistency for admin proxy trust."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.proxy_trust import DEFAULT_CLOUDFLARE_IPV4_CIDRS, DEFAULT_RENDER_TRUSTED_PROXY_CIDRS

REPO_ROOT = Path(__file__).resolve().parents[1]


def _render_yaml_text() -> str:
    return (REPO_ROOT / "render.yaml").read_text(encoding="utf-8")


def _env_value(document: str, key: str) -> str:
    pattern = rf"- key: {re.escape(key)}\s+value: \"([^\"]*)\""
    match = re.search(pattern, document)
    assert match, f"missing render.yaml env var {key}"
    return match.group(1)


@pytest.mark.unit
def test_render_start_command_disables_uvicorn_proxy_rewrite() -> None:
    document = _render_yaml_text()
    assert "startCommand: uvicorn app.main:app" in document
    assert "--no-proxy-headers" in document


@pytest.mark.unit
def test_render_declares_admin_proxy_trust_env() -> None:
    document = _render_yaml_text()
    assert _env_value(document, "ADMIN_TRUST_PROXY_HEADERS") == "true"
    trusted = _env_value(document, "ADMIN_TRUSTED_PROXY_CIDRS")
    assert "127.0.0.1" in trusted
    assert "10.0.0.0/8" in trusted
    for cidr in DEFAULT_RENDER_TRUSTED_PROXY_CIDRS:
        assert cidr in trusted
    for cidr in DEFAULT_CLOUDFLARE_IPV4_CIDRS:
        assert cidr in trusted


@pytest.mark.unit
def test_admin_auth_docs_match_render_proxy_settings() -> None:
    docs = (REPO_ROOT / "docs" / "ADMIN_AUTH.md").read_text(encoding="utf-8")
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in docs
    assert "--no-proxy-headers" in docs
    assert "right-to-left" in docs
