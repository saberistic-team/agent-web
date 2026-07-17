"""Browser tests for admin cache isolation (#337)."""

from __future__ import annotations

import re
from typing import Any

import pytest

from tests.test_admin_security_headers_browser import (
    LiveAdminServer,
    _authenticated_page,
    live_admin_server,
)

playwright_sync_api = pytest.importorskip(
    "playwright.sync_api", reason="playwright not installed; skipping browser suite"
)

pytestmark = pytest.mark.browser

ADMIN_CACHE_CONTROL = "no-store, private"
_OPERATOR_MARKER = re.compile(rf">\s*{re.escape('operator')}\s*<", re.IGNORECASE)


def _cache_control(response: Any) -> str:
    return (response.headers.get("cache-control") or "").lower()


def test_authenticated_admin_page_emits_no_store(
    live_admin_server: LiveAdminServer,
    browser: Any,
) -> None:
    context, page = _authenticated_page(live_admin_server, browser)
    try:
        response = page.goto(f"{live_admin_server.base_url}/admin")
        assert response is not None
        assert _cache_control(response) == ADMIN_CACHE_CONTROL
        page.wait_for_selector(".admin-main")
    finally:
        context.close()


def test_logout_then_reload_does_not_reuse_cached_admin_page(
    live_admin_server: LiveAdminServer,
    browser: Any,
) -> None:
    """Within HTTP cache guarantees, reload after logout must not reuse admin HTML."""
    context, page = _authenticated_page(live_admin_server, browser)
    try:
        first = page.goto(f"{live_admin_server.base_url}/admin")
        assert first is not None
        assert _cache_control(first) == ADMIN_CACHE_CONTROL
        page.wait_for_selector(".admin-main")
        assert _OPERATOR_MARKER.search(page.content()) is not None

        with page.expect_response(
            lambda response: response.request.method == "POST"
            and response.url.endswith("/admin/logout")
        ) as logout_response_info:
            page.locator("button.admin-signout").click()
        logout_response = logout_response_info.value
        assert _cache_control(logout_response) == ADMIN_CACHE_CONTROL
        page.wait_for_url("**/admin/login**")

        page.go_back()
        with page.expect_response(
            lambda response: response.request.method == "GET"
            and response.url.rstrip("/").endswith("/admin")
        ) as reload_response_info:
            page.reload()
        reload_response = reload_response_info.value
        assert _cache_control(reload_response) == ADMIN_CACHE_CONTROL
        assert page.url.endswith("/admin/login") or "Sign in" in page.content()
        assert _OPERATOR_MARKER.search(page.content()) is None
    finally:
        context.close()
