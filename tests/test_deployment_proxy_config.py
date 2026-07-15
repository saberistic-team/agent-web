"""Deployment configuration consistency for proxy trust (#239)."""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.unit
def test_render_yaml_declares_forwarded_allow_ips_and_uvicorn_flag() -> None:
    render_yaml = (ROOT / "render.yaml").read_text(encoding="utf-8")
    assert "FORWARDED_ALLOW_IPS" in render_yaml
    assert "--forwarded-allow-ips" in render_yaml
    assert '$FORWARDED_ALLOW_IPS' in render_yaml or "${FORWARDED_ALLOW_IPS}" in render_yaml


@pytest.mark.unit
def test_render_yaml_forwarded_allow_ips_not_wildcard() -> None:
    render_yaml = (ROOT / "render.yaml").read_text(encoding="utf-8")
    assert 'value: "*"' not in render_yaml
    assert "10.0.0.0/8" in render_yaml


@pytest.mark.unit
def test_admin_auth_docs_match_forwarded_allow_ips_model() -> None:
    docs = (ROOT / "docs" / "ADMIN_AUTH.md").read_text(encoding="utf-8")
    assert "FORWARDED_ALLOW_IPS" in docs
    assert "ADMIN_CLOUDFLARE_TRUSTED_CIDRS" in docs
    assert "ADMIN_TRUST_PROXY_HEADERS" not in docs.split("Do not re-enable legacy")[0]


@pytest.mark.unit
def test_health_reports_proxy_trust_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "10.0.0.0/8")
    client = TestClient(app)
    payload = client.get("/health").json()
    assert payload["proxy_trust_configured"] is True

    monkeypatch.delenv("FORWARDED_ALLOW_IPS", raising=False)
    payload = client.get("/health").json()
    assert payload["proxy_trust_configured"] is False
