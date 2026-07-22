"""Unit tests for admin cache isolation policy (#337)."""

from __future__ import annotations

import pytest
from starlette.responses import Response

from app.admin_response_policy import (
    ADMIN_CACHE_CONTROL,
    admin_cache_headers,
    apply_admin_cache_headers,
    apply_response_headers,
    is_admin_path,
)


@pytest.mark.unit
def test_admin_cache_headers_snapshot() -> None:
    headers = admin_cache_headers()
    assert headers == {"Cache-Control": ADMIN_CACHE_CONTROL}
    assert headers["Cache-Control"] == "no-store, private"


@pytest.mark.unit
@pytest.mark.parametrize(
    "path,expected",
    [
        ("/admin", True),
        ("/admin/", True),
        ("/admin/login", True),
        ("/admin/briefs/503", True),
        ("/assets/admin.css", False),
        ("/", False),
    ],
)
def test_is_admin_path_for_cache_policy(path: str, expected: bool) -> None:
    assert is_admin_path(path) is expected


@pytest.mark.unit
def test_apply_admin_cache_headers_replaces_weaker_directive() -> None:
    response = Response(status_code=200, headers={"Cache-Control": "public, max-age=3600"})
    apply_admin_cache_headers(response)
    assert response.headers["cache-control"] == ADMIN_CACHE_CONTROL


@pytest.mark.unit
def test_apply_response_headers_replaces_prior_value() -> None:
    response = Response(status_code=200, headers={"Cache-Control": "no-cache"})
    apply_response_headers(response, {"Cache-Control": ADMIN_CACHE_CONTROL})
    assert response.headers["cache-control"] == ADMIN_CACHE_CONTROL
    values = [value for name, value in response.headers.raw if name == b"cache-control"]
    assert len(values) == 1
