"""Unit tests for admin cache isolation policy (#337)."""

from __future__ import annotations

from collections import Counter

import pytest
from starlette.responses import Response

from app.admin_cache_policy import (
    ADMIN_CACHE_CONTROL,
    admin_cache_headers,
    apply_admin_cache_headers,
)


@pytest.mark.unit
def test_admin_cache_control_constant() -> None:
    assert ADMIN_CACHE_CONTROL == "no-store, private"
    assert admin_cache_headers() == {"Cache-Control": "no-store, private"}


@pytest.mark.unit
def test_apply_admin_cache_headers_replaces_weaker_directive() -> None:
    response = Response(status_code=200)
    response.headers["Cache-Control"] = "public, max-age=3600"
    apply_admin_cache_headers(response)
    assert response.headers["Cache-Control"] == "no-store, private"


@pytest.mark.unit
def test_apply_admin_cache_headers_replaces_no_cache() -> None:
    response = Response(status_code=200)
    response.headers["Cache-Control"] = "no-cache"
    apply_admin_cache_headers(response)
    assert response.headers["Cache-Control"] == "no-store, private"


@pytest.mark.unit
def test_apply_admin_cache_headers_emits_single_value() -> None:
    response = Response(status_code=200)
    apply_admin_cache_headers(response)
    assert response.headers.get("Cache-Control") == "no-store, private"
    raw_names = [name.decode("latin-1").lower() for name, _ in response.headers.raw]
    assert raw_names.count("cache-control") == 1
