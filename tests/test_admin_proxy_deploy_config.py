"""Deployment configuration tests for admin login proxy trust (#239)."""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.unit
def test_render_yaml_declares_proxy_trust_settings() -> None:
    render_yaml = (REPO_ROOT / "render.yaml").read_text()
    assert "forwarded-allow-ips=127.0.0.1" in render_yaml
    assert "ADMIN_LOGIN_TRUST_FORWARDED_HEADERS" in render_yaml
    assert "ADMIN_TRUST_PROXY_HEADERS" in render_yaml


@pytest.mark.unit
def test_admin_auth_docs_match_render_proxy_trust_model() -> None:
    docs = (REPO_ROOT / "docs" / "ADMIN_AUTH.md").read_text()
    assert "forwarded-allow-ips=127.0.0.1" in docs
    assert "ADMIN_LOGIN_TRUST_FORWARDED_HEADERS" in docs
    assert "right" in docs.lower()
    assert "never taken from the left-most raw header value" in docs
