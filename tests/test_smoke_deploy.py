"""Tests for production smoke deploy verification."""

from __future__ import annotations

import pytest

from scripts.smoke_deploy import verify_admin_login_source_trust


@pytest.mark.unit
def test_verify_admin_login_source_trust_accepts_render_config() -> None:
    payload = {
        "status": "ok",
        "admin_login_source_trust": {
            "trusted_proxies_configured": True,
            "trust_wildcard": True,
            "uvicorn_proxy_headers": True,
            "uvicorn_forwarded_allow_ips": "*",
            "resolution_mode": "trusted_hop_chain",
        },
    }
    assert verify_admin_login_source_trust(payload, "https://saberistic.com/health")


@pytest.mark.unit
def test_verify_admin_login_source_trust_rejects_missing_block() -> None:
    assert not verify_admin_login_source_trust({"status": "ok"}, "https://example.com/health")
