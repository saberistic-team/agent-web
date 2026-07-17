"""Tests for production smoke deploy verification."""

from __future__ import annotations

import pytest

from app.admin_response_policy import ADMIN_CACHE_CONTROL
from scripts.smoke_deploy import verify_admin_cache_headers, verify_admin_login_source_trust


@pytest.mark.unit
def test_verify_admin_login_source_trust_accepts_render_config() -> None:
    payload = {
        "status": "ok",
        "admin_proxy_trust": {
            "enabled": True,
            "trusted_proxy_entry_count": 3,
        },
    }
    assert verify_admin_login_source_trust(payload, "https://saberistic.com")


@pytest.mark.unit
def test_verify_admin_login_source_trust_rejects_missing_block() -> None:
    assert not verify_admin_login_source_trust({"status": "ok"}, "https://saberistic.com")


@pytest.mark.unit
def test_verify_admin_login_source_trust_skips_non_production_origin() -> None:
    assert verify_admin_login_source_trust({"status": "ok"}, "http://localhost:8000")


@pytest.mark.unit
def test_verify_admin_cache_headers_accepts_enforced_policy() -> None:
    assert verify_admin_cache_headers({"cache-control": ADMIN_CACHE_CONTROL})


@pytest.mark.unit
def test_verify_admin_cache_headers_rejects_missing_or_weak_policy() -> None:
    assert not verify_admin_cache_headers({})
    assert not verify_admin_cache_headers({"cache-control": "no-cache"})
    assert not verify_admin_cache_headers({"cache-control": "public, max-age=3600"})
