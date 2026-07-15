"""Deployment configuration consistency for proxy trust (#239)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.config import cloudflare_edge_networks, get_settings, trusted_proxy_networks
from app.proxy_trust import proxy_trust_health_summary

REPO_ROOT = Path(__file__).resolve().parent.parent
RENDER_YAML = REPO_ROOT / "render.yaml"
ADMIN_AUTH_DOC = REPO_ROOT / "docs" / "ADMIN_AUTH.md"


def _render_env_value(text: str, key: str) -> str:
    pattern = rf"- key: {re.escape(key)}\n\s+value: \"([^\"]*)\""
    match = re.search(pattern, text)
    assert match is not None, f"missing {key} in render.yaml"
    return match.group(1)


@pytest.mark.unit
def test_render_yaml_declares_proxy_trust_settings() -> None:
    text = RENDER_YAML.read_text()
    assert "startCommand: uvicorn app.main:app --host 0.0.0.0 --port $PORT --forwarded-allow-ips=" in text
    assert _render_env_value(text, "UVICORN_FORWARDED_ALLOW_IPS") == ""
    assert _render_env_value(text, "ADMIN_TRUST_CLOUDFLARE_EDGE") == "true"
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in text
    assert "ADMIN_CLOUDFLARE_EDGE_CIDRS" in text


@pytest.mark.unit
def test_render_start_command_matches_uvicorn_env(monkeypatch: pytest.MonkeyPatch) -> None:
    text = RENDER_YAML.read_text()
    monkeypatch.setenv(
        "UVICORN_FORWARDED_ALLOW_IPS",
        _render_env_value(text, "UVICORN_FORWARDED_ALLOW_IPS"),
    )
    monkeypatch.setenv(
        "ADMIN_TRUSTED_PROXY_CIDRS",
        _render_env_value(text, "ADMIN_TRUSTED_PROXY_CIDRS"),
    )
    monkeypatch.setenv(
        "ADMIN_TRUST_CLOUDFLARE_EDGE",
        _render_env_value(text, "ADMIN_TRUST_CLOUDFLARE_EDGE"),
    )
    monkeypatch.setenv(
        "ADMIN_CLOUDFLARE_EDGE_CIDRS",
        _render_env_value(text, "ADMIN_CLOUDFLARE_EDGE_CIDRS"),
    )

    settings = get_settings()
    assert settings.uvicorn_forwarded_allow_ips == ""
    assert trusted_proxy_networks(settings)
    assert cloudflare_edge_networks(settings)
    assert settings.admin_trust_cloudflare_edge is True


@pytest.mark.unit
def test_admin_auth_doc_documents_trusted_proxy_model() -> None:
    text = ADMIN_AUTH_DOC.read_text()
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in text
    assert "ADMIN_TRUST_PROXY_HEADERS" not in text
    assert "right-to-left" in text
    assert "GET /health" in text


@pytest.mark.unit
def test_health_proxy_trust_summary_has_no_raw_addresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", "10.0.0.0/8")
    monkeypatch.setenv("ADMIN_CLOUDFLARE_EDGE_CIDRS", "172.64.0.0/13")
    monkeypatch.setenv("ADMIN_TRUST_CLOUDFLARE_EDGE", "true")
    monkeypatch.setenv("UVICORN_FORWARDED_ALLOW_IPS", "")
    settings = get_settings()
    summary = proxy_trust_health_summary(
        trusted_networks=trusted_proxy_networks(settings),
        cloudflare_networks=cloudflare_edge_networks(settings),
        trust_cloudflare_edge=settings.admin_trust_cloudflare_edge,
        uvicorn_forwarded_allow_ips=settings.uvicorn_forwarded_allow_ips,
    )
    assert summary["trusted_proxy_configured"] is True
    assert summary["uvicorn_forwarded_allow_ips"] == ""
    serialized = str(summary)
    assert "10.0.0." not in serialized
    assert "172.64." not in serialized
