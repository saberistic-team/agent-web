"""Browser regression for admin cache isolation after logout (#337)."""

from __future__ import annotations

import re
from typing import Any

import pytest

pytest_plugins = ["tests.test_admin_security_headers_browser"]

playwright_sync_api = pytest.importorskip(
    "playwright.sync_api", reason="playwright not installed; skipping browser suite"
)

from app.admin_response_policy import ADMIN_CACHE_CONTROL  # noqa: E402

from tests.test_admin_security_headers_browser import (  # noqa: E402
    live_admin_preview_server,
)

pytestmark = pytest.mark.browser


def _extract_csrf_token(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match is not None
    return match.group(1)


def test_logout_back_navigation_respects_no_store_cache_policy(
    live_admin_preview_server: Any,
    browser: Any,
) -> None:
    """Verify admin responses are not reusable HTTP cache entries after logout.

    ``Cache-Control: no-store`` reduces storage/reuse by HTTP caches but does not
    guarantee erasure from browser UI memory (bfcache, screenshots, etc.).
    """
    context = browser.new_context()
    admin_cache_controls: list[str] = []

    def _record_admin_cache_control(response: Any) -> None:
        if "/admin" not in response.url:
            return
        cache_control = response.headers.get("cache-control", "")
        if cache_control:
            admin_cache_controls.append(cache_control)

    page = context.new_page()
    page.on("response", _record_admin_cache_control)
    try:
        context.add_cookies(
            [
                {
                    "name": name,
                    "value": value,
                    "domain": "127.0.0.1",
                    "path": "/admin",
                }
                for name, value in live_admin_preview_server.cookies.items()
            ]
        )
        page.goto(f"{live_admin_preview_server.base_url}/admin/briefs")
        page.wait_for_selector(".admin-main")

        logout_csrf = _extract_csrf_token(page.content())
        page.request.post(
            f"{live_admin_preview_server.base_url}/admin/logout",
            form={"csrf_token": logout_csrf},
        )

        page.go_back()
        page.reload(wait_until="networkidle")

        assert "/admin/login" in page.url
        assert all(value == ADMIN_CACHE_CONTROL for value in admin_cache_controls)
        assert admin_cache_controls
    finally:
        context.close()
