"""Browser tests for admin cache isolation (#337)."""

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
from app.admin_cache_policy import ADMIN_CACHE_CONTROL  # noqa: E402
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


@pytest.fixture
def live_admin_server(monkeypatch: pytest.MonkeyPatch) -> Generator[LiveAdminServer, None, None]:
    from argon2 import PasswordHasher

    password_hash = PasswordHasher().hash(TEST_PASSWORD)
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", password_hash)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("DATABASE_URL", "postgresql://test/db")
    monkeypatch.setenv("BASE_URL", "http://127.0.0.1")
    monkeypatch.delenv("ADMIN_PREVIEW_MODE", raising=False)

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

    def _revoke_session(conn: Any, *, session_id: int) -> None:
        for row in session_store.values():
            if row["id"] == session_id:
                row["revoked_at"] = datetime.now(timezone.utc)

    mock_conn = MagicMock()
    port = _free_port()

    with (
        patch.object(db, "get_admin_session_by_token_hash", side_effect=_get_session),
        patch.object(db, "update_admin_session_csrf", side_effect=_update_csrf),
        patch.object(db, "revoke_admin_session", side_effect=_revoke_session),
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


def _on_login_page(page: Any) -> bool:
    return "/admin/login" in page.url or page.locator("form.admin-form--compact").count() > 0


def test_authenticated_admin_page_emits_no_store_header(
    live_admin_server: LiveAdminServer,
    browser: Any,
) -> None:
    context, page = _authenticated_page(live_admin_server, browser)
    captured: list[str] = []

    def _capture(response: Any) -> None:
        if response.url.endswith("/admin") and response.request.method == "GET":
            captured.append(response.headers.get("cache-control", ""))

    page.on("response", _capture)
    try:
        page.goto(f"{live_admin_server.base_url}/admin")
        page.wait_for_selector(".admin-main")
        assert captured == [ADMIN_CACHE_CONTROL]
    finally:
        context.close()


def test_logout_back_navigation_does_not_reuse_authenticated_http_cache(
    live_admin_server: LiveAdminServer,
    browser: Any,
) -> None:
    """HTTP no-store limits disk/shared-cache reuse; bfcache/UI memory are out of scope."""
    context, page = _authenticated_page(live_admin_server, browser)
    try:
        page.goto(f"{live_admin_server.base_url}/admin")
        page.wait_for_selector(".admin-main")
        assert TEST_USERNAME in page.locator(".admin-main").inner_text()

        page.locator("form.admin-signout-form button[type='submit']").click()
        page.wait_for_url("**/admin/login**")
        assert _on_login_page(page)

        page.go_back()
        assert _on_login_page(page)

        page.reload()
        assert _on_login_page(page)
    finally:
        context.close()
