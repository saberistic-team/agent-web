"""Deployment configuration tests for admin proxy trust (#239)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
RENDER_YAML = REPO_ROOT / "render.yaml"
ADMIN_AUTH_DOC = REPO_ROOT / "docs" / "ADMIN_AUTH.md"

EXPECTED_TRUSTED_PROXIES = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.1,::1"


def _render_yaml_text() -> str:
    return RENDER_YAML.read_text(encoding="utf-8")


@pytest.mark.unit
def test_render_yaml_declares_matching_proxy_trust_settings() -> None:
    text = _render_yaml_text()
    start_match = re.search(r"startCommand:\s*>?-?\s*\n?\s*(.+)", text, re.DOTALL)
    assert start_match is not None
    start_command = " ".join(start_match.group(1).split())
    env_match = re.search(
        r"key:\s*ADMIN_TRUSTED_PROXY_IPS\s*\n\s*value:\s*\"([^\"]+)\"",
        text,
    )
    assert env_match is not None

    assert "--proxy-headers" in start_command
    assert "--forwarded-allow-ips" in start_command
    assert EXPECTED_TRUSTED_PROXIES in start_command
    assert env_match.group(1) == EXPECTED_TRUSTED_PROXIES


@pytest.mark.unit
def test_admin_auth_doc_documents_same_trust_model() -> None:
    text = ADMIN_AUTH_DOC.read_text(encoding="utf-8")
    assert "ADMIN_TRUSTED_PROXY_IPS" in text
    assert "--forwarded-allow-ips" in text
    assert "right-to-left" in text
    assert "admin_proxy_trust" in text


@pytest.mark.unit
def test_health_reports_proxy_trust_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", EXPECTED_TRUSTED_PROXIES)
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json().get("admin_proxy_trust") == "configured"
