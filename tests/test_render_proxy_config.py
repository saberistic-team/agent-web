"""Deployment configuration tests for admin proxy trust (#239)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app

REPO_ROOT = Path(__file__).resolve().parent.parent
RENDER_YAML = REPO_ROOT / "render.yaml"
ADMIN_AUTH_DOC = REPO_ROOT / "docs" / "ADMIN_AUTH.md"


@pytest.mark.unit
def test_render_start_command_configures_uvicorn_forwarded_allow_ips() -> None:
    content = RENDER_YAML.read_text(encoding="utf-8")
    assert "startCommand: uvicorn app.main:app" in content
    assert "--proxy-headers" in content
    assert "--forwarded-allow-ips=" in content
    assert "10.0.0.0/8" in content


@pytest.mark.unit
def test_render_env_vars_match_documented_proxy_trust_model() -> None:
    content = RENDER_YAML.read_text(encoding="utf-8")
    assert "key: ADMIN_TRUST_PROXY_HEADERS" in content
    assert 'value: "true"' in content
    assert "key: ADMIN_TRUSTED_PROXY_IPS" in content
    assert "10.0.0.0/8" in content
    assert "172.16.0.0/12" in content
    assert "192.168.0.0/16" in content


@pytest.mark.unit
def test_admin_auth_doc_documents_chain_and_rollback() -> None:
    doc = ADMIN_AUTH_DOC.read_text(encoding="utf-8")
    assert "Client → Cloudflare → Render load balancer → Uvicorn" in doc
    assert "ADMIN_TRUSTED_PROXY_IPS" in doc
    assert "right-to-left" in doc.lower()
    assert "ADMIN_TRUST_PROXY_HEADERS=false" in doc
    assert "--forwarded-allow-ips" in doc


@pytest.mark.unit
def test_health_reports_proxy_trust_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", "10.0.0.0/8,172.16.0.0/12")
    client = TestClient(app)
    payload = client.get("/health").json()
    assert payload["admin_proxy_trust"] == {
        "enabled": True,
        "trusted_proxy_entry_count": 2,
    }
