"""Browser tests for admin cache isolation after logout (#337)."""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Generator, Iterator
from unittest.mock import MagicMock, patch

import pytest

playwright_sync_api = pytest.importorskip(
    "playwright.sync_api", reason="playwright not installed; skipping browser suite"
)
sync_playwright = playwright_sync_api.sync_playwright

import uvicorn  # noqa: E402

from app import admin_auth, db  # noqa: E402
from app.admin_response_policy import ADMIN_CACHE_CONTROL  # noqa: E402
from app.main import app  # noqa: E402

pytestmark = pytest.mark.browser

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_SECRET = "test-session-secret-32chars-minimum"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class LiveAdminServer:
    base_url: str
    cookies: dict[str, str]


def _start_live_admin_server(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[LiveAdminServer, None, None]:
    from argon2 import PasswordHasher

    password_hash = PasswordHasher().hash(TEST_PASSWORD)
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", password_hash)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("DATABASE_URL", "postgresql://test/db")
    monkeypatch.setenv("BASE_URL", "http://127.0.0.1")

    raw_token = admin_auth.generate_session_token()
    csrf_raw = admin_auth.generate_csrf_value()
    token_hash = admin_auth.hash_session_token(raw_token)
    csrf_hash = admin_auth.hash_csrf_token(csrf_raw)
    session_store: dict[str, dict[str, Any]] = {
        token_hash: {
            "id": 1,
            "token_hash": token_hash,
            "admin_username": TEST_USERNAME,
            "created_at": datetime.now(timezone.utc),
            "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
            "revoked_at": None,
            "csrf_token_hash": csrf_hash,
        }
    }

    def _get_session(conn: Any, th: str) -> dict[str, Any] | None:
        return session_store.get(th)

    def _update_csrf(conn: Any, *, session_id: int, csrf_token_hash: str) -> None:
        for row in session_store.values():
            if row["id"] == session_id:
                row["csrf_token_hash"] = csrf_token_hash

    mock_conn = MagicMock()
    port = _free_port()

    with (
        patch.object(db, "get_admin_session_by_token_hash", side_effect=_get_session),
        patch.object(db, "update_admin_session_csrf", side_effect=_update_csrf),
        patch.object(db, "init_db"),
        patch("app.db.db_connection") as db_conn,
        patch("app.admin_routes.db.db_connection", db_conn),
    ):
        db_conn.return_value.__enter__.return_value = mock_conn

        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.started, "in-process admin server failed to start"

        try:
            yield LiveAdminServer(
                base_url=f"http://127.0.0.1:{port}",
                cookies={admin_auth.SESSION_COOKIE_NAME: raw_token},
            )
        finally:
            server.should_exit = True
            thread.join(timeout=10)


@pytest.fixture
def live_admin_server(monkeypatch: pytest.MonkeyPatch) -> Generator[LiveAdminServer, None, None]:
    monkeypatch.delenv("ADMIN_PREVIEW_MODE", raising=False)
    yield from _start_live_admin_server(monkeypatch)


@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            yield browser
        finally:
            browser.close()


def _authenticated_page(live_admin_server: LiveAdminServer, browser: Any) -> tuple[Any, Any]:
    context = browser.new_context()
    context.add_cookies(
        [
            {
                "name": name,
                "value": value,
                "domain": "127.0.0.1",
                "path": "/admin",
            }
            for name, value in live_admin_server.cookies.items()
        ]
    )
    page = context.new_page()
    return context, page


def test_admin_document_responses_emit_no_store(
    live_admin_server: LiveAdminServer, browser: Any
) -> None:
    context, page = _authenticated_page(live_admin_server, browser)
    document_cache_controls: list[str] = []

    def _on_response(response: Any) -> None:
        if response.request.resource_type != "document":
            return
        if not response.url.startswith(f"{live_admin_server.base_url}/admin"):
            return
        document_cache_controls.append(response.headers.get("cache-control", ""))

    page.on("response", _on_response)
    try:
        page.goto(f"{live_admin_server.base_url}/admin")
        page.wait_for_selector(".admin-main")
        assert document_cache_controls
        assert all(value == ADMIN_CACHE_CONTROL for value in document_cache_controls)
    finally:
        context.close()


def test_logout_back_navigation_does_not_reuse_http_cached_admin_shell(
    live_admin_server: LiveAdminServer, browser: Any
) -> None:
    """After logout, back navigation must not restore an authenticated admin shell.

    ``Cache-Control: no-store`` prevents HTTP cache storage/reuse but does not
    erase browser UI memory, screenshots, or OS swap.
    """
    context, page = _authenticated_page(live_admin_server, browser)
    try:
        page.goto(f"{live_admin_server.base_url}/admin/imports")
        page.wait_for_selector("#linkedin-import-form")
        assert page.locator('form[action="/admin/logout"]').count() == 1

        logout_form = page.locator('form[action="/admin/logout"]')
        with page.expect_navigation():
            logout_form.locator('button[type="submit"]').click()
        page.wait_for_selector("form.admin-form--compact")
        assert page.url.endswith("/admin/login")

        page.go_back()
        page.wait_for_load_state("domcontentloaded")

        # Revoked session: must not show authenticated imports surface again.
        assert page.locator("#linkedin-import-form").count() == 0
        assert page.locator("form.admin-form--compact").count() == 1
    finally:
        context.close()


def test_reload_after_logout_fetches_fresh_login_not_cached_dashboard(
    live_admin_server: LiveAdminServer, browser: Any
) -> None:
    context, page = _authenticated_page(live_admin_server, browser)
    try:
        page.goto(f"{live_admin_server.base_url}/admin")
        page.wait_for_selector(".admin-main")

        logout_form = page.locator('form[action="/admin/logout"]')
        with page.expect_navigation():
            logout_form.locator('button[type="submit"]').click()
        page.wait_for_selector("form.admin-form--compact")

        page.reload()
        page.wait_for_selector("form.admin-form--compact")
        assert page.locator(".admin-main h1").count() == 0
        assert page.locator("form.admin-form--compact").count() == 1
    finally:
        context.close()
