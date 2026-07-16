"""Browser regression for admin no-store cache policy (#337).

Verifies that authenticated admin pages carry ``Cache-Control: no-store, private``
and that back navigation after logout does not reuse a stored HTTP cache entry
for privileged content. This does not claim control over browser UI memory,
bfcache, or OS-level retention.
"""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Generator, Iterator
from unittest.mock import MagicMock, patch

import pytest
import uvicorn

playwright_sync_api = pytest.importorskip(
    "playwright.sync_api", reason="playwright not installed; skipping browser suite"
)
sync_playwright = playwright_sync_api.sync_playwright

from app import admin_auth, db
from app.admin_auth import SESSION_COOKIE_NAME
from app.admin_routes import PREVIEW_SESSION_TOKEN
from app.config import get_settings

pytestmark = pytest.mark.browser

AUTHENTICATED_MARKER = "Preview data — not production"
LOGIN_PATH_FRAGMENT = "/admin/login"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class LiveAdminServer:
    base_url: str
    cookies: dict[str, str]
    admin_cache_headers: list[str] = field(default_factory=list)


@pytest.fixture
def live_preview_admin_server(monkeypatch: pytest.MonkeyPatch) -> Generator[LiveAdminServer, None, None]:
    monkeypatch.setenv("ADMIN_USERNAME", "operator")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", "unused")
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "test-session-secret-32chars-minimum")
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "42")
    monkeypatch.setenv("BASE_URL", "http://127.0.0.1")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    raw_token = PREVIEW_SESSION_TOKEN
    csrf_raw = admin_auth.generate_csrf_value()
    token_hash = admin_auth.hash_session_token(raw_token)
    csrf_hash = admin_auth.hash_csrf_token(csrf_raw)
    session_store: dict[str, dict[str, Any]] = {
        token_hash: {
            "id": 1,
            "token_hash": token_hash,
            "admin_username": "preview",
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
    base_url = f"http://127.0.0.1:{port}"
    monkeypatch.setenv("BASE_URL", base_url)

    from app.main import app

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
                base_url=base_url,
                cookies={SESSION_COOKIE_NAME: raw_token},
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


def _preview_csrf() -> str:
    return admin_auth.derive_session_csrf_token(PREVIEW_SESSION_TOKEN, get_settings())


@pytest.mark.browser
def test_logout_back_navigation_respects_no_store(
    live_preview_admin_server: LiveAdminServer,
    browser: Any,
) -> None:
    context = browser.new_context()
    try:
        context.add_cookies(
            [
                {
                    "name": name,
                    "value": value,
                    "url": live_preview_admin_server.base_url,
                }
                for name, value in live_preview_admin_server.cookies.items()
            ]
        )
        page = context.new_page()
        captured_headers: list[str] = []

        def _capture_admin_cache(response: Any) -> None:
            if "/admin/" in response.url:
                cache_control = response.headers.get("cache-control", "")
                if cache_control:
                    captured_headers.append(cache_control)

        page.on("response", _capture_admin_cache)

        page.goto(f"{live_preview_admin_server.base_url}/admin/briefs")
        assert AUTHENTICATED_MARKER in page.content()

        assert captured_headers, "expected Cache-Control on authenticated admin response"
        assert all(value == "no-store, private" for value in captured_headers)

        page.request.post(
            f"{live_preview_admin_server.base_url}/admin/logout",
            form={"csrf_token": _preview_csrf()},
        )
        page.goto(f"{live_preview_admin_server.base_url}/admin/login")
        assert LOGIN_PATH_FRAGMENT in page.url

        page.go_back()
        page.wait_for_timeout(300)

        body_after_back = page.content()
        assert AUTHENTICATED_MARKER not in body_after_back
        assert LOGIN_PATH_FRAGMENT in page.url or "Sign in" in body_after_back

        page.reload()
        assert AUTHENTICATED_MARKER not in page.content()
    finally:
        context.close()
