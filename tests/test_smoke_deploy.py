"""Tests for production smoke deploy verification."""

from __future__ import annotations

import pytest

from scripts.smoke_deploy import verify_admin_login_cache_policy, verify_admin_login_source_trust


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
def test_verify_admin_login_cache_policy_accepts_no_store() -> None:
    class _FakeResponse:
        headers = {"Cache-Control": "no-store, private"}

    class _FakeContext:
        def __enter__(self):
            return _FakeResponse()

        def __exit__(self, *args: object) -> None:
            return None

    class _FakeUrlopen:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def __call__(self, *args: object, **kwargs: object) -> _FakeContext:
            return _FakeContext()

    import scripts.smoke_deploy as smoke_deploy

    original = smoke_deploy.urllib.request.urlopen
    smoke_deploy.urllib.request.urlopen = _FakeUrlopen()  # type: ignore[assignment]
    try:
        ok, detail = verify_admin_login_cache_policy("https://saberistic.com")
    finally:
        smoke_deploy.urllib.request.urlopen = original
    assert ok is True
    assert "no-store, private" in detail


@pytest.mark.unit
def test_verify_admin_login_cache_policy_rejects_missing_header() -> None:
    class _FakeResponse:
        headers: dict[str, str] = {}

    class _FakeContext:
        def __enter__(self):
            return _FakeResponse()

        def __exit__(self, *args: object) -> None:
            return None

    import scripts.smoke_deploy as smoke_deploy

    original = smoke_deploy.urllib.request.urlopen
    smoke_deploy.urllib.request.urlopen = lambda *args, **kwargs: _FakeContext()  # type: ignore[assignment]
    try:
        ok, detail = verify_admin_login_cache_policy("https://saberistic.com")
    finally:
        smoke_deploy.urllib.request.urlopen = original
    assert ok is False
    assert "Cache-Control" in detail
