"""Browser tests for admin CSP enforcement (#308)."""

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
def live_admin_preview_server(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[LiveAdminServer, None, None]:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "42")
    yield from _start_live_admin_server(monkeypatch)


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


def test_admin_page_blocks_framing(
    live_admin_server: LiveAdminServer, browser: Any
) -> None:
    context, page = _authenticated_page(live_admin_server, browser)
    try:
        page.goto(f"{live_admin_server.base_url}/admin/imports")
        page.wait_for_selector("#linkedin-import-form")
        blocked = page.evaluate(
            """() => {
              const iframe = document.createElement('iframe');
              iframe.src = '/admin/imports';
              document.body.appendChild(iframe);
              return new Promise((resolve) => {
                let settled = false;
                const finish = (value) => {
                  if (!settled) {
                    settled = true;
                    resolve(value);
                  }
                };
                iframe.addEventListener('load', () => {
                  try {
                    const doc = iframe.contentDocument;
                    finish(!(doc && doc.getElementById('linkedin-import-form')));
                  } catch (err) {
                    finish(true);
                  }
                });
                iframe.addEventListener('error', () => finish(true));
                window.setTimeout(() => finish(true), 1500);
              });
            }"""
        )
        assert blocked is True
    finally:
        context.close()


def test_injected_inline_script_without_nonce_is_blocked(
    live_admin_server: LiveAdminServer, browser: Any
) -> None:
    context, page = _authenticated_page(live_admin_server, browser)
    try:
        page.goto(f"{live_admin_server.base_url}/admin/imports")
        page.wait_for_selector("#linkedin-import-form")
        page.evaluate(
            """() => {
              const script = document.createElement('script');
              script.textContent = 'window.__csp_probe = 1';
              document.body.appendChild(script);
            }"""
        )
        assert page.evaluate("() => window.__csp_probe") is None
    finally:
        context.close()


def test_imports_flow_has_no_csp_console_violations(
    live_admin_server: LiveAdminServer, browser: Any
) -> None:
    context, page = _authenticated_page(live_admin_server, browser)
    violations: list[str] = []

    def _on_console(msg: Any) -> None:
        text = msg.text
        if "Content Security Policy" in text or "Refused to" in text:
            violations.append(text)

    page.on("console", _on_console)
    try:
        page.goto(f"{live_admin_server.base_url}/admin/imports")
        page.wait_for_selector("#linkedin-import-form")
        assert page.locator('script[src="/assets/linkedin-import.js"]').count() == 1
        assert violations == []
    finally:
        context.close()


@pytest.mark.parametrize(
    "path,selector",
    [
        ("/admin/login", "form.admin-form--compact"),
        ("/admin", ".admin-main"),
        ("/admin/briefs", ".admin-main"),
        ("/admin/companies", ".admin-main"),
        ("/admin/contacts", ".admin-main"),
        ("/admin/pipeline", ".admin-main"),
        ("/admin/imports", ".admin-main"),
        ("/admin/audit", ".admin-main"),
    ],
)
def test_critical_admin_pages_have_no_csp_console_violations(
    live_admin_preview_server: LiveAdminServer,
    browser: Any,
    path: str,
    selector: str,
) -> None:
    context = browser.new_context()
    violations: list[str] = []

    def _on_console(msg: Any) -> None:
        text = msg.text
        if "Content Security Policy" in text or "Refused to" in text:
            violations.append(text)

    page = context.new_page()
    page.on("console", _on_console)
    try:
        page.goto(f"{live_admin_preview_server.base_url}{path}")
        page.wait_for_selector(selector)
        assert violations == [], f"{path} triggered CSP violations: {violations}"
    finally:
        context.close()


def test_camera_geolocation_microphone_unavailable(
    live_admin_server: LiveAdminServer, browser: Any
) -> None:
    context, page = _authenticated_page(live_admin_server, browser)
    try:
        page.goto(f"{live_admin_server.base_url}/admin")
        page.wait_for_selector(".admin-main")
        denied = page.evaluate(
            """async () => {
              const checks = [];
              try {
                await navigator.mediaDevices.getUserMedia({ audio: true, video: true });
                checks.push('media:allowed');
              } catch (err) {
                checks.push('media:denied');
              }
              try {
                await new Promise((resolve, reject) => {
                  navigator.geolocation.getCurrentPosition(resolve, reject, { timeout: 500 });
                });
                checks.push('geo:allowed');
              } catch (err) {
                checks.push('geo:denied');
              }
              return checks;
            }"""
        )
        assert "media:denied" in denied
        assert "geo:denied" in denied
    finally:
        context.close()
