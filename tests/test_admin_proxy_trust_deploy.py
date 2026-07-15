"""Deployment configuration consistency for admin proxy trust (#239)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.admin_client_source import TRUST_MODEL_VERSION
from app.config import get_settings

REPO_ROOT = Path(__file__).resolve().parents[1]
RENDER_YAML = REPO_ROOT / "render.yaml"
ADMIN_AUTH_DOC = REPO_ROOT / "docs" / "ADMIN_AUTH.md"

EXPECTED_FORWARDED_ALLOW_IPS = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.1,::1"


@pytest.mark.unit
def test_render_start_command_configures_forwarded_allow_ips_explicitly() -> None:
    text = RENDER_YAML.read_text(encoding="utf-8")
    assert "startCommand:" in text
    assert "--forwarded-allow-ips" in text
    assert "10.0.0.0/8" in text
    assert "--proxy-headers" not in text


@pytest.mark.unit
def test_render_env_vars_match_forwarded_allow_ips() -> None:
    text = RENDER_YAML.read_text(encoding="utf-8")
    assert "ADMIN_TRUST_PROXY_HEADERS" in text
    assert 'value: "true"' in text
    assert "UVICORN_FORWARDED_ALLOW_IPS" in text
    assert EXPECTED_FORWARDED_ALLOW_IPS in text


@pytest.mark.unit
def test_admin_auth_doc_documents_verified_hop_model() -> None:
    text = ADMIN_AUTH_DOC.read_text(encoding="utf-8")
    assert "verified-hop" in text or "right-to-left" in text
    assert "ADMIN_TRUSTED_PROXY_IPS" in text
    assert "UVICORN_FORWARDED_ALLOW_IPS" in text
    assert "--proxy-headers" in text
    assert "CF-Connecting-IP" in text
    assert "rollback" in text.lower()


@pytest.mark.unit
def test_uvicorn_forwarded_allow_ips_env_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UVICORN_FORWARDED_ALLOW_IPS", EXPECTED_FORWARDED_ALLOW_IPS)
    settings = get_settings()
    assert settings.uvicorn_forwarded_allow_ips == EXPECTED_FORWARDED_ALLOW_IPS


@pytest.mark.unit
def test_health_trust_model_version_constant() -> None:
    assert TRUST_MODEL_VERSION == "verified-hop-v1"
