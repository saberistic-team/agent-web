"""Tests for production smoke deploy verification."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from scripts.smoke_deploy import (
    ADMIN_CACHE_CONTROL,
    verify_admin_cache_control,
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
def test_verify_admin_cache_control_accepts_no_store_private() -> None:
    response = MagicMock()
    response.headers = {"Cache-Control": ADMIN_CACHE_CONTROL}
    response.__enter__ = MagicMock(return_value=response)
    response.__exit__ = MagicMock(return_value=False)
    with patch("scripts.smoke_deploy.urllib.request.urlopen", return_value=response):
        ok, detail = verify_admin_cache_control("https://saberistic.com")
    assert ok is True
    assert ADMIN_CACHE_CONTROL in detail


@pytest.mark.unit
def test_verify_admin_cache_control_rejects_missing_header() -> None:
    response = MagicMock()
    response.headers = {}
    response.__enter__ = MagicMock(return_value=response)
    response.__exit__ = MagicMock(return_value=False)
    with patch("scripts.smoke_deploy.urllib.request.urlopen", return_value=response):
        ok, detail = verify_admin_cache_control("https://saberistic.com")
    assert ok is False
    assert "cache-control=''" in detail
