"""Deployment configuration consistency for admin proxy trust (#239)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

RENDER_PATH = Path("render.yaml")
ADMIN_AUTH_DOC = Path("docs/ADMIN_AUTH.md")


@pytest.mark.unit
def test_render_yaml_declares_proxy_trust_env_and_uvicorn_flag() -> None:
    text = RENDER_PATH.read_text(encoding="utf-8")
    assert re.search(
        r'startCommand:.*--forwarded-allow-ips="\$UVICORN_FORWARDED_ALLOW_IPS"',
        text,
    )
    for key, expected in (
        ("ADMIN_TRUST_PROXY_HEADERS", "true"),
        ("ADMIN_TRUST_CLOUDFLARE_EDGE", "true"),
    ):
        assert re.search(rf"key: {key}\n\s+value: \"{expected}\"", text)
    for key in ("ADMIN_TRUSTED_PROXY_CIDRS", "UVICORN_FORWARDED_ALLOW_IPS"):
        assert re.search(rf"key: {key}\n\s+value:", text)


@pytest.mark.unit
def test_admin_auth_doc_documents_same_trust_model() -> None:
    text = ADMIN_AUTH_DOC.read_text(encoding="utf-8")
    for phrase in (
        "ADMIN_TRUSTED_PROXY_CIDRS",
        "ADMIN_TRUST_CLOUDFLARE_EDGE",
        "UVICORN_FORWARDED_ALLOW_IPS",
        "Right-to-left",
        "admin_client_source_trust",
        "Rollback if every request shares one limiter source",
    ):
        assert phrase in text
