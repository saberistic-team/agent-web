"""Deployment configuration tests for admin proxy trust (#239)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.admin_client_source import (
    DEFAULT_RENDER_TRUSTED_PROXY_CIDRS,
    uvicorn_forwarded_allow_ips_arg,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _render_env_value(render_yaml: str, key: str) -> str:
    match = re.search(
        rf"- key: {re.escape(key)}\n\s+value: \"([^\"]+)\"",
        render_yaml,
    )
    assert match is not None, f"missing {key} in render.yaml"
    return match.group(1)


@pytest.mark.unit
def test_render_yaml_declares_matching_proxy_trust_settings() -> None:
    render_yaml = (REPO_ROOT / "render.yaml").read_text()
    start_match = re.search(r"startCommand: (.+)", render_yaml)
    assert start_match is not None
    start_command = start_match.group(1)

    assert "--forwarded-allow-ips" in start_command
    uvicorn_cidrs = start_command.split("--forwarded-allow-ips", 1)[1].strip()
    assert uvicorn_cidrs == uvicorn_forwarded_allow_ips_arg()

    configured = _render_env_value(render_yaml, "ADMIN_TRUSTED_PROXY_CIDRS")
    assert configured == uvicorn_forwarded_allow_ips_arg()
    assert configured == ",".join(DEFAULT_RENDER_TRUSTED_PROXY_CIDRS)


@pytest.mark.unit
def test_admin_auth_docs_mention_trusted_proxy_cidrs() -> None:
    docs = (REPO_ROOT / "docs" / "ADMIN_AUTH.md").read_text()
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in docs
    assert "right-to-left" in docs
    assert "--forwarded-allow-ips" in docs
