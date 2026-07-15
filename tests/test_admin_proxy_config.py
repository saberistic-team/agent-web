"""Deployment configuration consistency for admin proxy trust."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.proxy_trust import DEFAULT_TRUSTED_PROXY_IPS

REPO_ROOT = Path(__file__).resolve().parents[1]
RENDER_YAML = REPO_ROOT / "render.yaml"
ADMIN_AUTH_DOC = REPO_ROOT / "docs" / "ADMIN_AUTH.md"
ASGI_MODULE = REPO_ROOT / "app" / "asgi.py"


def _render_yaml_text() -> str:
    return RENDER_YAML.read_text(encoding="utf-8")


def _env_value(text: str, key: str) -> str | None:
    match = re.search(
        rf"- key: {re.escape(key)}\s+value: \"([^\"]+)\"",
        text,
    )
    return match.group(1) if match else None


@pytest.mark.unit
def test_render_start_command_uses_asgi_entrypoint() -> None:
    text = _render_yaml_text()
    assert "app.asgi:app" in text
    assert "app.main:app" not in text
    assert "--no-proxy-headers" in text


@pytest.mark.unit
def test_render_env_declares_proxy_trust_settings() -> None:
    text = _render_yaml_text()
    assert _env_value(text, "ADMIN_TRUST_PROXY_HEADERS") == "true"
    trusted = _env_value(text, "ADMIN_TRUSTED_PROXY_IPS") or ""
    for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10"):
        assert cidr in trusted


@pytest.mark.unit
def test_asgi_module_applies_proxy_headers_middleware() -> None:
    text = ASGI_MODULE.read_text(encoding="utf-8")
    assert "ImmediatePeerMiddleware" in text
    assert "ProxyHeadersMiddleware" in text
    assert "admin_trusted_proxy_ips" in text


@pytest.mark.unit
def test_admin_auth_doc_documents_proxy_trust_model() -> None:
    doc = ADMIN_AUTH_DOC.read_text(encoding="utf-8")
    assert "ADMIN_TRUSTED_PROXY_IPS" in doc
    assert "app.asgi:app" in doc
    assert "ProxyHeadersMiddleware" in doc
    assert "Cloudflare" in doc
    assert "resolution_path" in doc
    assert "--no-proxy-headers" in doc


@pytest.mark.unit
def test_default_trusted_proxy_ips_matches_render_private_ranges() -> None:
    for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10"):
        assert cidr in DEFAULT_TRUSTED_PROXY_IPS
