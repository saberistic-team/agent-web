"""Deployment configuration checks for admin proxy trust (#239)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
RENDER_YAML = REPO_ROOT / "render.yaml"
ADMIN_AUTH_DOC = REPO_ROOT / "docs" / "ADMIN_AUTH.md"


@pytest.mark.unit
def test_render_yaml_declares_proxy_trust_env_and_uvicorn_boundary() -> None:
    payload = yaml.safe_load(RENDER_YAML.read_text(encoding="utf-8"))
    service = payload["services"][0]
    start_command = service["startCommand"]
    env = {item["key"]: item.get("value") for item in service["envVars"] if "key" in item}

    assert "--forwarded-allow-ips=" in start_command
    assert "uvicorn app.main:app" in start_command
    assert env.get("ADMIN_TRUST_PROXY_HEADERS") == "true"
    assert env.get("ADMIN_TRUSTED_PROXY_CIDRS")
    assert "10.0.0.0/8" in env["ADMIN_TRUSTED_PROXY_CIDRS"]


@pytest.mark.unit
def test_admin_auth_doc_matches_render_proxy_settings() -> None:
    doc = ADMIN_AUTH_DOC.read_text(encoding="utf-8")
    render = yaml.safe_load(RENDER_YAML.read_text(encoding="utf-8"))
    start_command = render["services"][0]["startCommand"]

    assert "ADMIN_TRUSTED_PROXY_CIDRS" in doc
    assert "ADMIN_TRUST_PROXY_HEADERS" in doc
    assert "--forwarded-allow-ips" in doc
    assert "right-to-left" in doc.lower() or "right-to-left" in doc
    assert "Cloudflare" in doc
    assert start_command.split("--forwarded-allow-ips=")[1].split("'")[0] in doc or (
        "10.0.0.0/8" in doc
    )


@pytest.mark.unit
def test_render_forwarded_allow_ips_aligns_with_trusted_peer_cidrs() -> None:
    payload = yaml.safe_load(RENDER_YAML.read_text(encoding="utf-8"))
    service = payload["services"][0]
    start_command = service["startCommand"]
    env = {item["key"]: item.get("value") for item in service["envVars"] if "key" in item}

    forwarded_allow = start_command.split("--forwarded-allow-ips=")[1].strip().strip("'")
    trusted_peer = env["ADMIN_TRUSTED_PROXY_CIDRS"]

    for cidr in ("10.0.0.0/8", "127.0.0.1", "::1"):
        assert cidr in forwarded_allow
        assert cidr in trusted_peer
