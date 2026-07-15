"""Deployment configuration tests for admin login proxy trust."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.admin_client_source import default_trusted_proxy_entries
from scripts import smoke_deploy

REPO_ROOT = Path(__file__).resolve().parent.parent
RENDER_YAML = REPO_ROOT / "render.yaml"
ADMIN_AUTH_DOC = REPO_ROOT / "docs" / "ADMIN_AUTH.md"


def _render_service_config() -> tuple[str, dict[str, str]]:
    text = RENDER_YAML.read_text(encoding="utf-8")
    start_match = re.search(r"startCommand:\s*>?-\s*\n?\s*(.+)", text, re.DOTALL)
    assert start_match is not None
    start_command = " ".join(start_match.group(1).split())
    env: dict[str, str] = {}
    for key, value in re.findall(
        r"- key: (ADMIN_[A-Z_]+)\n\s+value: \"([^\"]*)\"",
        text,
    ):
        env[key] = value
    return start_command, env


@pytest.mark.unit
def test_render_yaml_declares_proxy_trust_settings() -> None:
    start_command, env = _render_service_config()

    assert "--proxy-headers" in start_command
    assert "--forwarded-allow-ips=" in start_command
    assert "10.0.0.0/8" in start_command
    assert env["ADMIN_TRUST_PROXY_HEADERS"] == "true"


@pytest.mark.unit
def test_render_start_command_matches_internal_trusted_defaults() -> None:
    start_command, _env = _render_service_config()
    for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.1/32"):
        assert cidr in start_command


@pytest.mark.unit
def test_admin_auth_doc_matches_runtime_trust_model() -> None:
    doc = ADMIN_AUTH_DOC.read_text(encoding="utf-8")
    assert "Cloudflare → Render load balancer → Uvicorn" in doc
    assert "ADMIN_TRUST_PROXY_HEADERS" in doc
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in doc
    assert "admin_client_source_trust" in doc
    assert "right-to-left" in doc
    assert "left-most raw ``X-Forwarded-For``" in doc


@pytest.mark.unit
def test_default_trusted_proxy_entries_include_internal_and_cloudflare_ranges() -> None:
    entries = default_trusted_proxy_entries()
    assert "10.0.0.0/8" in entries
    assert "172.64.0.0/13" in entries


@pytest.mark.unit
def test_smoke_deploy_health_check_accepts_trust_mode_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_get_json(url: str) -> dict:
        calls.append(url)
        if url.endswith("/health"):
            return {"status": "ok", "admin_client_source_trust": "verified-proxy-hops"}
        return {"message": "hello world"}

    monkeypatch.setattr(smoke_deploy, "get_json", fake_get_json)
    assert smoke_deploy.main(["--base-url", "https://example.com"]) == 0
    assert calls[0].endswith("/health")
