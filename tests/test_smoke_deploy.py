"""Tests for production smoke deploy verification."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from scripts.smoke_deploy import (
    ADMIN_CACHE_CONTROL,
    verify_admin_login_cache_headers,
    verify_admin_login_source_trust,
)


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
def test_verify_admin_login_cache_headers_accepts_no_store() -> None:
    headers = {"cache-control": ADMIN_CACHE_CONTROL}
    with patch("scripts.smoke_deploy.head_response_headers", return_value=headers):
        ok, detail = verify_admin_login_cache_headers("https://saberistic.com")
    assert ok is True
    assert detail == ADMIN_CACHE_CONTROL


@pytest.mark.unit
def test_verify_admin_login_cache_headers_rejects_missing() -> None:
    with patch("scripts.smoke_deploy.head_response_headers", return_value={}):
        ok, detail = verify_admin_login_cache_headers("https://saberistic.com")
    assert ok is False
    assert "cache-control=''" in detail


@pytest.mark.unit
def test_verify_admin_login_cache_headers_rejects_weaker_policy() -> None:
    headers = {"cache-control": "no-cache"}
    with patch("scripts.smoke_deploy.head_response_headers", return_value=headers):
        ok, detail = verify_admin_login_cache_headers("https://saberistic.com")
    assert ok is False
    assert "no-cache" in detail
