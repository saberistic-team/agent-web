"""Unit tests for admin cache policy helpers (#337)."""

from __future__ import annotations

import pytest
from starlette.responses import Response

from app.admin_cache_policy import (
    ADMIN_CACHE_CONTROL,
    apply_admin_cache_headers,
    is_admin_path,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/admin", True),
        ("/admin/", True),
        ("/admin/login", True),
        ("/admin/briefs/42", True),
        ("/admin/pipeline", True),
        ("/admin/api/imports/linkedin/commit", True),
        ("/", False),
        ("/about", False),
        ("/assets/admin.css", False),
        ("/health", False),
        ("/administration", False),
    ],
)
def test_is_admin_path(path: str, expected: bool) -> None:
    assert is_admin_path(path) is expected


def test_apply_admin_cache_headers_sets_policy() -> None:
    response = Response(content="ok")
    apply_admin_cache_headers(response)
    assert response.headers.get("Cache-Control") == ADMIN_CACHE_CONTROL


def test_apply_admin_cache_headers_replaces_weaker_directive() -> None:
    response = Response(content="ok", headers={"Cache-Control": "max-age=3600, public"})
    apply_admin_cache_headers(response)
    assert response.headers.get("Cache-Control") == ADMIN_CACHE_CONTROL
    assert response.headers.getlist("cache-control") == [ADMIN_CACHE_CONTROL]


def test_apply_admin_cache_headers_replaces_no_cache() -> None:
    response = Response(content="ok", headers={"Cache-Control": "no-cache"})
    apply_admin_cache_headers(response)
    assert response.headers.get("Cache-Control") == ADMIN_CACHE_CONTROL
