"""Deployment parity tests for admin login proxy trust settings (#239)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.admin_client_source import PRODUCTION_TRUSTED_PROXY_IPS

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parent.parent
RENDER_YAML = REPO_ROOT / "render.yaml"
ADMIN_AUTH_DOC = REPO_ROOT / "docs" / "ADMIN_AUTH.md"


def test_render_yaml_declares_trusted_proxy_ips_and_uvicorn_flag() -> None:
    text = RENDER_YAML.read_text(encoding="utf-8")
    assert "ADMIN_TRUSTED_PROXY_IPS" in text
    assert PRODUCTION_TRUSTED_PROXY_IPS in text
    assert "--forwarded-allow-ips=" in text
    assert "127.0.0.1" in text
    assert "10.0.0.0/8" in text

    env_value = _render_env_value(text, "ADMIN_TRUSTED_PROXY_IPS")
    start_command = _render_start_command(text)
    forwarded_ips = _extract_forwarded_allow_ips(start_command)
    assert env_value == PRODUCTION_TRUSTED_PROXY_IPS
    assert forwarded_ips == PRODUCTION_TRUSTED_PROXY_IPS


def test_admin_auth_doc_documents_trust_model() -> None:
    text = ADMIN_AUTH_DOC.read_text(encoding="utf-8")
    assert "ADMIN_TRUSTED_PROXY_IPS" in text
    assert "--forwarded-allow-ips" in text
    assert "Cloudflare" in text
    assert "right-to-left" in text
    assert "unknown-trusted-proxy" in text
    assert "tests/test_admin_client_source.py" in text


def _render_env_value(render_yaml: str, key: str) -> str:
    lines = render_yaml.splitlines()
    for index, line in enumerate(lines):
        if f"key: {key}" in line:
            for follow in lines[index + 1 : index + 4]:
                if follow.strip().startswith("value:"):
                    return follow.split("value:", 1)[1].strip().strip('"')
    raise AssertionError(f"{key} env entry not found in render.yaml")


def _render_start_command(render_yaml: str) -> str:
    for line in render_yaml.splitlines():
        if line.strip().startswith("startCommand:"):
            return line.split("startCommand:", 1)[1].strip()
    raise AssertionError("startCommand not found in render.yaml")


def _extract_forwarded_allow_ips(start_command: str) -> str:
    marker = "--forwarded-allow-ips="
    if marker not in start_command:
        raise AssertionError("startCommand missing --forwarded-allow-ips")
    fragment = start_command.split(marker, 1)[1]
    if fragment.startswith("'"):
        return fragment.split("'", 2)[1]
    if fragment.startswith('"'):
        return fragment.split('"', 2)[1]
    return fragment.split()[0]
