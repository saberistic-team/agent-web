"""Deployment configuration tests for admin client-source proxy trust."""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
RENDER_YAML = REPO_ROOT / "render.yaml"
ADMIN_AUTH_DOC = REPO_ROOT / "docs" / "ADMIN_AUTH.md"

PRODUCTION_TRUSTED_IPS = (
    "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.1,::1"
)


@pytest.mark.unit
def test_render_yaml_declares_no_proxy_headers_and_trusted_ips() -> None:
    text = RENDER_YAML.read_text(encoding="utf-8")
    assert "--no-proxy-headers" in text
    assert "ADMIN_TRUSTED_PROXY_IPS" in text
    assert PRODUCTION_TRUSTED_IPS in text


@pytest.mark.unit
def test_admin_auth_doc_matches_render_proxy_trust_model() -> None:
    text = ADMIN_AUTH_DOC.read_text(encoding="utf-8")
    assert "--no-proxy-headers" in text
    assert "ADMIN_TRUSTED_PROXY_IPS" in text
    assert "verified-proxy-hop-v1" in text
    assert "ADMIN_TRUST_PROXY_HEADERS" not in text
    assert "left-most" not in text


@pytest.mark.unit
def test_health_reports_client_source_trust_fingerprint() -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    payload = client.get("/health").json()
    assert payload["client_source_trust"] == "verified-proxy-hop-v1"
