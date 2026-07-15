"""Deployment configuration consistency for admin proxy trust (#239)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
RENDER_YAML = REPO_ROOT / "render.yaml"
ADMIN_AUTH_DOC = REPO_ROOT / "docs" / "ADMIN_AUTH.md"

TRUSTED_PROXY_CIDRS = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.1/32"


def _render_yaml_text() -> str:
    return RENDER_YAML.read_text(encoding="utf-8")


def _env_value(text: str, key: str) -> str | None:
    pattern = rf"- key: {re.escape(key)}\n\s+value: \"?([^\"\n]+)\"?"
    match = re.search(pattern, text)
    return match.group(1) if match else None


@pytest.mark.unit
def test_render_start_command_sets_forwarded_allow_ips() -> None:
    text = _render_yaml_text()
    start_match = re.search(r"startCommand:\s*(.+)", text)
    assert start_match is not None
    start = start_match.group(1)
    assert "--forwarded-allow-ips=" in start
    for cidr in TRUSTED_PROXY_CIDRS.split(","):
        assert cidr in start


@pytest.mark.unit
def test_render_env_proxy_trust_matches_start_command() -> None:
    text = _render_yaml_text()
    assert _env_value(text, "ADMIN_TRUST_PROXY_HEADERS") == "true"
    assert _env_value(text, "ADMIN_TRUSTED_PROXY_CIDRS") == TRUSTED_PROXY_CIDRS
    assert _env_value(text, "ADMIN_CLOUDFLARE_PROXY_CIDRS") is not None
    for cidr in TRUSTED_PROXY_CIDRS.split(","):
        assert cidr in text


@pytest.mark.unit
def test_admin_auth_doc_documents_proxy_trust_model() -> None:
    text = ADMIN_AUTH_DOC.read_text(encoding="utf-8")
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in text
    assert "ADMIN_CLOUDFLARE_PROXY_CIDRS" in text
    assert "forwarded-allow-ips" in text
    assert "rightmost untrusted" in text
    assert "Rollback if proxy trust is misconfigured" in text
