"""Deployment configuration consistency for admin proxy trust."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.admin_client_source import default_trusted_proxy_cidrs

REPO_ROOT = Path(__file__).resolve().parent.parent
RENDER_YAML = REPO_ROOT / "render.yaml"


def _render_service_block() -> str:
    text = RENDER_YAML.read_text(encoding="utf-8")
    match = re.search(
        r"services:\s*\n\s*-\s*type:\s*web\s*\n(?P<body>(?:\s+.+\n)+)",
        text,
    )
    assert match is not None
    return match.group("body")


@pytest.mark.unit
def test_render_yaml_declares_trusted_proxy_settings() -> None:
    block = _render_service_block()
    assert "--forwarded-allow-ips=" in block
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in block
    assert "10.0.0.0/8" in block
    assert "127.0.0.1/32" in block


@pytest.mark.unit
def test_render_start_command_and_env_cidrs_align() -> None:
    block = _render_service_block()
    env_match = re.search(
        r"ADMIN_TRUSTED_PROXY_CIDRS\s*\n\s*value:\s*\"([^\"]+)\"",
        block,
    )
    assert env_match is not None
    env_cidrs = env_match.group(1)
    start_match = re.search(r"startCommand:\s*(.+)", block)
    assert start_match is not None
    start_command = start_match.group(1)

    for token in env_cidrs.split(","):
        normalized = token.strip().replace("/32", "")
        assert normalized in start_command or token.strip() in start_command


@pytest.mark.unit
def test_default_trusted_proxy_cidrs_match_render_private_ranges() -> None:
    defaults = default_trusted_proxy_cidrs()
    assert "10.0.0.0/8" in defaults
    assert "172.16.0.0/12" in defaults
    assert "192.168.0.0/16" in defaults
