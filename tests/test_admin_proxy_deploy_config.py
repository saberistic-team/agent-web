"""Deployment configuration consistency for admin proxy trust (#239)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.proxy_trust_config import DEFAULT_UVICORN_FORWARDED_ALLOW_IPS

REPO_ROOT = Path(__file__).resolve().parents[1]
RENDER_YAML = REPO_ROOT / "render.yaml"
ADMIN_AUTH_DOC = REPO_ROOT / "docs" / "ADMIN_AUTH.md"


def _render_yaml_text() -> str:
    return RENDER_YAML.read_text(encoding="utf-8")


@pytest.mark.unit
def test_render_yaml_declares_proxy_trust_env_and_uvicorn_boundary() -> None:
    text = _render_yaml_text()
    assert "startCommand:" in text
    assert "--forwarded-allow-ips" in text
    assert "10.0.0.0/8" in text
    assert re.search(r'ADMIN_TRUST_PROXY_HEADERS\s*\n\s*value:\s*"true"', text)
    assert "UVICORN_FORWARDED_ALLOW_IPS" in text


@pytest.mark.unit
def test_render_uvicorn_allow_ips_matches_documented_default() -> None:
    text = _render_yaml_text()
    match = re.search(
        r"UVICORN_FORWARDED_ALLOW_IPS\s*\n\s*value:\s*\"([^\"]+)\"",
        text,
    )
    assert match is not None
    assert match.group(1) == DEFAULT_UVICORN_FORWARDED_ALLOW_IPS
    for cidr in DEFAULT_UVICORN_FORWARDED_ALLOW_IPS.split(","):
        assert cidr in text


@pytest.mark.unit
def test_admin_auth_doc_documents_trust_model_and_rollback() -> None:
    text = ADMIN_AUTH_DOC.read_text(encoding="utf-8")
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in text
    assert "right-to-left" in text.lower()
    assert "CF-Connecting-IP" in text
    assert "admin_client_source" in text
    assert "Rollback" in text or "rollback" in text
    assert "Do not" in text and "proxy-headers" in text


@pytest.mark.unit
def test_health_endpoint_exposes_admin_client_source_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    payload = client.get("/health").json()
    summary = payload.get("admin_client_source")
    assert summary is not None
    assert summary["trust_enabled"] is True
    assert summary["trusted_proxy_cidr_count"] >= 1
    assert "203.0.113" not in str(summary)
