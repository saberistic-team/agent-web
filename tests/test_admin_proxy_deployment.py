"""Deployment configuration tests for admin login proxy trust."""

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
        rf"- key: {re.escape(key)}\n\s+value: \"([^\"]+)\"",
        text,
    )
    assert match is not None, f"{key} missing from render.yaml"
    return match.group(1)


@pytest.mark.unit
def test_render_yaml_declares_proxy_trust_settings() -> None:
    text = RENDER_YAML.read_text(encoding="utf-8")
    assert "--forwarded-allow-ips=" in text
    assert "10.0.0.0/8" in text
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in text
    assert "ADMIN_TRUSTED_EDGE_CIDRS" in text
    assert "10.0.0.0/8" in _render_env_value("ADMIN_TRUSTED_PROXY_CIDRS")
    assert "173.245.48.0/20" in _render_env_value("ADMIN_TRUSTED_EDGE_CIDRS")


@pytest.mark.unit
def test_render_and_docs_describe_same_trust_model() -> None:
    render = RENDER_YAML.read_text(encoding="utf-8")
    docs = ADMIN_AUTH_DOC.read_text(encoding="utf-8")
    assert "--forwarded-allow-ips" in render
    assert "--forwarded-allow-ips" in docs
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in render
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in docs
    assert "ADMIN_TRUSTED_EDGE_CIDRS" in render
    assert "ADMIN_TRUSTED_EDGE_CIDRS" in docs
    assert "right-to-left" in docs


@pytest.mark.unit
def test_health_reports_proxy_trust_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    from app.admin_client_source import reset_trusted_network_cache
    from app.config import get_settings
    from app.main import app

    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", "10.0.0.0/8")
    monkeypatch.setenv("ADMIN_TRUSTED_EDGE_CIDRS", "198.51.100.0/24")
    reset_trusted_network_cache()

    client = TestClient(app)
    payload = client.get("/health").json()
    trust = payload["admin_login_proxy_trust"]
    assert trust["trusted_proxy_cidrs_configured"] is True
    assert trust["trusted_edge_cidrs_configured"] is True
    assert "10.0.0.0/8" not in str(payload)


@pytest.mark.unit
def test_smoke_deploy_can_verify_health_proxy_trust_flag() -> None:
    script = (REPO_ROOT / "scripts" / "smoke_deploy.py").read_text(encoding="utf-8")
    assert "/health" in script
