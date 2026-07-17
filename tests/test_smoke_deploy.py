"""Tests for production smoke deploy verification."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from scripts.smoke_deploy import (
    ADMIN_CACHE_CONTROL,
    fetch_response_headers,
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
def test_verify_admin_cache_control_accepts_policy() -> None:
    headers = {"cache-control": ADMIN_CACHE_CONTROL}
    with patch("scripts.smoke_deploy.fetch_response_headers", return_value=headers):
        assert verify_admin_cache_control("https://saberistic.com")


@pytest.mark.unit
def test_verify_admin_cache_control_rejects_missing_header() -> None:
    with patch("scripts.smoke_deploy.fetch_response_headers", return_value={}):
        assert not verify_admin_cache_control("https://saberistic.com")


@pytest.mark.unit
def test_verify_admin_cache_control_skips_non_production_origin() -> None:
    assert verify_admin_cache_control("http://localhost:8000")


@pytest.mark.unit
def test_fetch_response_headers_uses_head_without_body() -> None:
    class _FakeResponse:
        headers = {"Cache-Control": ADMIN_CACHE_CONTROL}

        def __enter__(self) -> "_FakeResponse":
            return self

        def __exit__(self, *args: object) -> None:
            return None

    with patch("scripts.smoke_deploy.urllib.request.urlopen", return_value=_FakeResponse()):
        headers = fetch_response_headers("https://saberistic.com/admin/login")
    assert headers["cache-control"] == ADMIN_CACHE_CONTROL
