"""Deployment configuration consistency for admin proxy trust (#239)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _render_yaml_text() -> str:
    return (REPO_ROOT / "render.yaml").read_text()


def _env_value(text: str, key: str) -> str | None:
    pattern = rf"- key: {re.escape(key)}\n\s+value: \"([^\"]*)\""
    match = re.search(pattern, text)
    return match.group(1) if match else None


@pytest.mark.unit
def test_render_yaml_declares_proxy_trust_env_and_uvicorn_boundary() -> None:
    text = _render_yaml_text()
    start_command_match = re.search(r"startCommand: (.+)", text)
    assert start_command_match is not None
    start_command = start_command_match.group(1)

    assert "--forwarded-allow-ips" in start_command
    assert "127.0.0.1" in start_command

    trusted_cidrs = _env_value(text, "ADMIN_TRUSTED_PROXY_CIDRS")
    assert trusted_cidrs
    assert "10.0.0.0/8" in trusted_cidrs
    assert _env_value(text, "ADMIN_TRUST_CLOUDFLARE_PROXY") == "true"


@pytest.mark.unit
def test_admin_auth_docs_match_render_proxy_model() -> None:
    docs = (REPO_ROOT / "docs" / "ADMIN_AUTH.md").read_text()
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in docs
    assert "ADMIN_TRUST_CLOUDFLARE_PROXY" in docs
    assert "--forwarded-allow-ips 127.0.0.1" in docs
    assert "right-to-left" in docs.lower() or "right to left" in docs.lower()


@pytest.mark.unit
def test_legacy_admin_trust_proxy_headers_maps_to_render_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    from app.config import get_settings

    settings = get_settings()
    assert settings.admin_trusted_proxy_cidrs
    assert "10.0.0.0/8" in settings.admin_trusted_proxy_cidrs
