"""Browser tests for admin cache isolation (#337)."""

from __future__ import annotations

from typing import Any

import pytest

from tests.test_admin_security_headers_browser import (
    LiveAdminServer,
    _authenticated_page,
    browser,
    live_admin_server,
)

pytestmark = pytest.mark.browser

ADMIN_CACHE_CONTROL = "no-store, private"


def test_authenticated_admin_response_is_not_http_cacheable(
    live_admin_server: LiveAdminServer, browser: Any
) -> None:
    context, page = _authenticated_page(live_admin_server, browser)
    captured: list[str] = []

    def _on_response(response: Any) -> None:
        if response.url.rstrip("/").endswith("/admin"):
            captured.append(response.headers.get("cache-control", ""))

    page.on("response", _on_response)
    try:
        page.goto(f"{live_admin_server.base_url}/admin")
        page.wait_for_selector(".admin-main")
        assert captured
        for cache_control in captured:
            assert cache_control == ADMIN_CACHE_CONTROL
    finally:
        context.close()


def test_logout_blocks_http_cache_reuse_on_back_navigation(
    live_admin_server: LiveAdminServer, browser: Any
) -> None:
    """Verify no-store prevents reusable HTTP cache entries after logout.

    Browser back/forward cache (bfcache) and in-memory UI state are outside
    HTTP cache guarantees; this test only asserts cache-control policy and that
    a cache-only fetch cannot reuse a stored admin document.
    """
    context, page = _authenticated_page(live_admin_server, browser)
    try:
        page.goto(f"{live_admin_server.base_url}/admin")
        page.wait_for_selector(".admin-main")
        assert page.locator(".admin-main").is_visible()

        logout_csrf = page.locator('input[name="csrf_token"]').first.input_value()
        page.evaluate(
            """({ csrfToken }) => {
              const form = document.querySelector('form[action="/admin/logout"]');
              if (!form) {
                throw new Error('logout form missing');
              }
              form.querySelector('input[name="csrf_token"]').value = csrfToken;
              form.submit();
            }""",
            {"csrfToken": logout_csrf},
        )
        page.wait_for_url("**/admin/login")

        cache_only = page.evaluate(
            """async () => {
              try {
                const response = await fetch('/admin', { cache: 'only-if-cached' });
                return { ok: response.ok, status: response.status };
              } catch (error) {
                return { error: String(error) };
              }
            }"""
        )
        assert not (cache_only.get("ok") is True and cache_only.get("status") == 200)

        page.go_back()
        page.wait_for_load_state("domcontentloaded")
        # Without a valid session, reload must not serve authenticated admin HTML.
        page.reload()
        page.wait_for_url("**/admin/login*")
        assert page.locator("form.admin-form--compact").is_visible()
    finally:
        context.close()
