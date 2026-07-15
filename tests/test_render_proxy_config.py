"""Deployment configuration consistency for trusted admin login sources (#239)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import get_settings


REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.unit
def test_render_yaml_declares_proxy_trust_settings() -> None:
    render_yaml = (REPO_ROOT / "render.yaml").read_text(encoding="utf-8")
    assert "--forwarded-allow-ips" in render_yaml
    assert "$UVICORN_FORWARDED_ALLOW_IPS" in render_yaml
    assert "UVICORN_FORWARDED_ALLOW_IPS" in render_yaml
    assert 'value: "127.0.0.1"' in render_yaml
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in render_yaml
    assert "10.0.0.0/8" in render_yaml


@pytest.mark.unit
def test_admin_auth_docs_document_proxy_trust_model() -> None:
    docs = (REPO_ROOT / "docs" / "ADMIN_AUTH.md").read_text(encoding="utf-8")
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in docs
    assert "UVICORN_FORWARDED_ALLOW_IPS" in docs
    assert "Cloudflare" in docs
    assert "the left-most ``X-Forwarded-For`` value is used" not in docs


@pytest.mark.unit
def test_settings_default_uvicorn_forwarded_allow_ips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("UVICORN_FORWARDED_ALLOW_IPS", raising=False)
    settings = get_settings()
    assert settings.uvicorn_forwarded_allow_ips == "127.0.0.1"
