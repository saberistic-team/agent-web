"""Unit tests for admin cache isolation policy (#337)."""

from __future__ import annotations

import pytest
from starlette.responses import Response

from app.admin_cache_policy import (
    ADMIN_CACHE_CONTROL,
    admin_cache_headers,
    apply_admin_cache_headers,
)


@pytest.mark.unit
def test_admin_cache_headers_snapshot() -> None:
    headers = admin_cache_headers()
    assert headers == {"Cache-Control": ADMIN_CACHE_CONTROL}
    assert headers["Cache-Control"] == "no-store, private"


@pytest.mark.unit
def test_apply_admin_cache_headers_replaces_weaker_directive() -> None:
    response = Response()
    response.headers["Cache-Control"] = "public, max-age=3600"
    apply_admin_cache_headers(response)
    assert response.headers["Cache-Control"] == "no-store, private"


@pytest.mark.unit
def test_apply_admin_cache_headers_replaces_no_cache_directive() -> None:
    response = Response()
    response.headers["Cache-Control"] = "no-cache"
    apply_admin_cache_headers(response)
    assert response.headers["Cache-Control"] == "no-store, private"


@pytest.mark.unit
def test_apply_admin_cache_headers_emits_single_value() -> None:
    response = Response()
    response.headers["Cache-Control"] = "public"
    apply_admin_cache_headers(response)
    assert list(response.headers.keys()).count("cache-control") == 1
    assert response.headers["Cache-Control"] == "no-store, private"
