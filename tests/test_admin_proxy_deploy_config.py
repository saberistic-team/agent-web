"""Deployment configuration checks for admin proxy trust (#239)."""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
RENDER_YAML = REPO_ROOT / "render.yaml"
ADMIN_AUTH_DOC = REPO_ROOT / "docs" / "ADMIN_AUTH.md"

DOCUMENTED_FORWARDED_ALLOW_IPS = (
    "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.1,::1"
)
PRODUCTION_TRUSTED_CIDRS = (
    "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.1/32,::1/128"
)


@pytest.mark.unit
def test_render_yaml_declares_admin_proxy_trust_settings() -> None:
    content = RENDER_YAML.read_text(encoding="utf-8")
    assert 'key: ADMIN_TRUST_PROXY_HEADERS' in content
    assert 'value: "true"' in content
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in content
    for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"):
        assert cidr in content


@pytest.mark.unit
def test_render_start_command_keeps_uvicorn_proxy_headers_disabled() -> None:
    content = RENDER_YAML.read_text(encoding="utf-8")
    assert "startCommand: uvicorn app.main:app --host 0.0.0.0 --port $PORT" in content
    assert "--proxy-headers" not in content


@pytest.mark.unit
def test_admin_auth_doc_matches_render_proxy_trust_model() -> None:
    doc = ADMIN_AUTH_DOC.read_text(encoding="utf-8")
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in doc
    assert "ADMIN_TRUST_PROXY_HEADERS" in doc
    assert "right-to-left" in doc.lower() or "rightmost untrusted hop" in doc.lower()
    assert DOCUMENTED_FORWARDED_ALLOW_IPS in doc
    assert "without" in doc.lower() and "--proxy-headers" in doc
    assert PRODUCTION_TRUSTED_CIDRS.split(",")[0] in doc
