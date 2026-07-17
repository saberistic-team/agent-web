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
    assert admin_cache_headers() == {"Cache-Control": ADMIN_CACHE_CONTROL}
    assert ADMIN_CACHE_CONTROL == "no-store, private"


@pytest.mark.unit
def test_apply_admin_cache_headers_replaces_weaker_directive() -> None:
    response = Response(status_code=200)
    response.headers["Cache-Control"] = "public, max-age=3600"
    apply_admin_cache_headers(response)
    assert response.headers["Cache-Control"] == ADMIN_CACHE_CONTROL


@pytest.mark.unit
def test_apply_admin_cache_headers_emits_single_value() -> None:
    response = Response(status_code=200)
    apply_admin_cache_headers(response)
    cache_values = [value for key, value in response.headers.raw if key == b"cache-control"]
    assert cache_values == [ADMIN_CACHE_CONTROL.encode()]
