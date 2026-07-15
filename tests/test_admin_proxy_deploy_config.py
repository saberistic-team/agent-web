"""Deployment configuration tests for admin login proxy trust (#239)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
RENDER_YAML = REPO_ROOT / "render.yaml"
ADMIN_AUTH_DOC = REPO_ROOT / "docs" / "ADMIN_AUTH.md"


def _render_env_value(key: str) -> str:
    text = RENDER_YAML.read_text(encoding="utf-8")
    match = re.search(
        rf"- key: {re.escape(key)}\s+value: \"([^\"]+)\"",
        text,
    )
    assert match is not None, f"missing render.yaml env var {key}"
    return match.group(1)


@pytest.mark.unit
def test_render_yaml_pins_proxy_trust_settings() -> None:
    render = RENDER_YAML.read_text(encoding="utf-8")

    assert "startCommand: uvicorn app.main:app" in render
    assert "--forwarded-allow-ips=127.0.0.1" in render

    assert _render_env_value("ADMIN_TRUST_PROXY_HEADERS") == "true"
    trusted_cidrs = _render_env_value("ADMIN_TRUSTED_PROXY_CIDRS")
    assert "10.0.0.0/8" in trusted_cidrs
    assert "172.16.0.0/12" in trusted_cidrs


@pytest.mark.unit
def test_admin_auth_doc_matches_render_proxy_configuration() -> None:
    doc = ADMIN_AUTH_DOC.read_text(encoding="utf-8")
    render = RENDER_YAML.read_text(encoding="utf-8")

    assert "--forwarded-allow-ips=127.0.0.1" in doc
    assert "--forwarded-allow-ips=127.0.0.1" in render
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in doc
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in render
    assert "right-to-left" in doc
    assert "source_resolution_path" in doc
