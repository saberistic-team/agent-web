"""Deployment configuration consistency for admin proxy trust."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
RENDER_YAML = REPO_ROOT / "render.yaml"

EXPECTED_FORWARDED_ALLOW_IPS = (
    "10.0.0.0/8,100.64.0.0/10,172.16.0.0/12,192.168.0.0/16,127.0.0.1,::1"
)


def _render_service() -> dict:
    payload = yaml.safe_load(RENDER_YAML.read_text(encoding="utf-8"))
    services = payload.get("services") or []
    assert services, "render.yaml must declare at least one service"
    return services[0]


def _env_map(service: dict) -> dict[str, str]:
    env: dict[str, str] = {}
    for item in service.get("envVars") or []:
        key = item.get("key")
        if key and "value" in item:
            env[key] = str(item["value"])
    return env


@pytest.mark.unit
def test_render_start_command_configures_uvicorn_forwarded_allow_ips() -> None:
    service = _render_service()
    start_command = service.get("startCommand", "")
    assert "--forwarded-allow-ips" in start_command
    assert "10.0.0.0/8" in start_command
    assert "*" not in start_command


@pytest.mark.unit
def test_render_env_vars_match_uvicorn_and_application_trust() -> None:
    service = _render_service()
    env = _env_map(service)
    assert env.get("FORWARDED_ALLOW_IPS") == EXPECTED_FORWARDED_ALLOW_IPS
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in env
    assert "10.0.0.0/8" in env["ADMIN_TRUSTED_PROXY_CIDRS"]
    assert env.get("ADMIN_TRUST_PROXY_HEADERS") is None


@pytest.mark.unit
def test_admin_auth_docs_document_proxy_trust_model() -> None:
    docs = (REPO_ROOT / "docs" / "ADMIN_AUTH.md").read_text(encoding="utf-8")
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in docs
    assert "FORWARDED_ALLOW_IPS" in docs
    assert "Cloudflare" in docs
    assert "left-most" in docs.lower() or "left-most" in docs
