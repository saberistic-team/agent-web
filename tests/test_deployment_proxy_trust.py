"""Deployment configuration tests for admin proxy trust (#239)."""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
RENDER_YAML = REPO_ROOT / "render.yaml"
ADMIN_AUTH_DOC = REPO_ROOT / "docs" / "ADMIN_AUTH.md"

EXPECTED_TRUSTED = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.1"


@pytest.mark.unit
def test_render_yaml_declares_explicit_proxy_trust_settings() -> None:
    text = RENDER_YAML.read_text(encoding="utf-8")
    assert "uvicorn app.main:app --host 0.0.0.0 --port $PORT" in text
    assert "--proxy-headers" not in text
    assert "ADMIN_TRUST_PROXY_HEADERS" in text
    assert "ADMIN_TRUSTED_PROXY_IPS" in text
    assert "FORWARDED_ALLOW_IPS" in text
    assert f'value: "{EXPECTED_TRUSTED}"' in text
    assert 'value: "true"' in text


@pytest.mark.unit
def test_admin_auth_doc_matches_render_proxy_trust_model() -> None:
    text = ADMIN_AUTH_DOC.read_text(encoding="utf-8")
    assert "ADMIN_TRUSTED_PROXY_IPS" in text
    assert "FORWARDED_ALLOW_IPS" in text
    assert "Right-to-left X-Forwarded-For" in text
    assert "CF-Connecting-IP" in text
    assert "Rollback / recovery" in text
    assert "without" in text and "--proxy-headers" in text
