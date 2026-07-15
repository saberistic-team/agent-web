"""Deployment configuration checks for admin proxy trust (#239)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
RENDER_YAML = REPO_ROOT / "render.yaml"
ADMIN_AUTH_DOC = REPO_ROOT / "docs" / "ADMIN_AUTH.md"

EXPECTED_TRUSTED_CIDRS = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,100.64.0.0/10"


@pytest.mark.unit
def test_render_yaml_declares_explicit_proxy_trust_settings() -> None:
    payload = yaml.safe_load(RENDER_YAML.read_text(encoding="utf-8"))
    service = payload["services"][0]
    start_command = service["startCommand"]
    assert "--no-proxy-headers" in start_command
    assert "uvicorn app.main:app" in start_command

    env = {item["key"]: item.get("value") for item in service["envVars"] if "key" in item}
    assert env["ADMIN_TRUSTED_PROXY_IPS"] == EXPECTED_TRUSTED_CIDRS
    assert env["FORWARDED_ALLOW_IPS"] == EXPECTED_TRUSTED_CIDRS


@pytest.mark.unit
def test_admin_auth_doc_matches_render_proxy_trust_model() -> None:
    text = ADMIN_AUTH_DOC.read_text(encoding="utf-8")
    assert "ADMIN_TRUSTED_PROXY_IPS" in text
    assert "FORWARDED_ALLOW_IPS" in text
    assert "--no-proxy-headers" in text
    assert "Cloudflare edge" in text
    assert "right-to-left" in text.lower() or "right-to-left" in text
    assert "ADMIN_TRUST_PROXY_HEADERS" not in text
