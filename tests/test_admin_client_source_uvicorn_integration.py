"""Uvicorn integration tests for admin login client source resolution."""

from __future__ import annotations

import socket
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Generator, Iterator
from unittest.mock import MagicMock, patch

import httpx
import pytest
import uvicorn

from app import db
from app.admin_auth import build_source_rate_limit_key
from app.main import app
from app.proxy_trust_config import production_trusted_proxy_cidrs

CLIENT_A = "203.0.113.50"
SPOOFED = "203.0.113.99"
LOCAL_TRUSTED_PEER = "127.0.0.1"


class _MemoryRateLimitStore:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    def try_admit(
        self,
        limiter_keys: tuple[str, ...],
        now: datetime,
        *,
        rate_limit: int,
        window_seconds: int,
        lockout_seconds: int,
    ) -> db.AdminLoginAdmission:
        ordered_keys = tuple(sorted(limiter_keys))
        for limiter_key in ordered_keys:
            if limiter_key not in self.rows:
                self.rows[limiter_key] = {
                    "failure_count": 0,
                    "window_started_at": now,
                    "locked_until": None,
                }

        for limiter_key in ordered_keys:
            row = self.rows[limiter_key]
            locked_until = row.get("locked_until")
            if locked_until is not None and locked_until > now:
                return db.AdminLoginAdmission(
                    admitted=False,
                    throttled=True,
                    already_locked=True,
                    lockout_transition=False,
                )

        lockout_transition = False
        for limiter_key in ordered_keys:
            row = self.rows[limiter_key]
            prior_count = row["failure_count"]
            row["failure_count"] = prior_count + 1
            if row["failure_count"] >= rate_limit:
                row["locked_until"] = now + timedelta(seconds=lockout_seconds)
                if prior_count < rate_limit:
                    lockout_transition = True

        return db.AdminLoginAdmission(
            admitted=True,
            throttled=False,
            already_locked=False,
            lockout_transition=lockout_transition,
        )

    def cleanup(self, *args: Any, **kwargs: Any) -> int:
        return 0


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextmanager
def _patched_rate_limiter(store: _MemoryRateLimitStore) -> Generator[None, None, None]:
    def try_admit(
        conn: Any,
        *,
        limiter_keys: tuple[str, ...],
        now: datetime,
        rate_limit: int,
        window_seconds: int,
        lockout_seconds: int,
    ) -> db.AdminLoginAdmission:
        return store.try_admit(
            limiter_keys,
            now,
            rate_limit=rate_limit,
            window_seconds=window_seconds,
            lockout_seconds=lockout_seconds,
        )

    with (
        patch("app.admin_auth.db.try_admit_admin_login", side_effect=try_admit),
        patch("app.admin_auth.db.cleanup_expired_admin_login_rate_limits", return_value=0),
        patch("app.admin_auth.db.db_connection") as db_conn,
    ):
        db_conn.return_value.__enter__.return_value = MagicMock()
        db_conn.return_value.__exit__.return_value = None
        yield


@pytest.fixture
def uvicorn_server(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", "operator")
    monkeypatch.setenv(
        "ADMIN_PASSWORD_HASH",
        "$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$RdescudvJCsgt3ub+b+dWRWJTmaaJObG",
    )
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "test-session-secret-32chars-minimum")
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", production_trusted_proxy_cidrs())
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")

    with patch("app.main.db.init_db", return_value=None):
        port = _free_port()
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            proxy_headers=True,
            forwarded_allow_ips=production_trusted_proxy_cidrs(),
        )
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()

        base_url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                response = httpx.get(f"{base_url}/health", timeout=0.5)
                if response.status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.05)
        else:
            server.should_exit = True
            thread.join(timeout=2)
            pytest.fail("uvicorn server did not become ready")

        try:
            yield base_url
        finally:
            server.should_exit = True
            thread.join(timeout=2)


@pytest.mark.integration
def test_uvicorn_proxy_config_resolves_trusted_xff_chain(uvicorn_server: str) -> None:
    store = _MemoryRateLimitStore()
    with _patched_rate_limiter(store):
        headers = {"X-Forwarded-For": f"{CLIENT_A}, {LOCAL_TRUSTED_PEER}"}
        with httpx.Client(base_url=uvicorn_server, timeout=2.0) as client:
            for _ in range(2):
                response = client.post(
                    "/admin/login",
                    data={
                        "username": "ghost",
                        "password": "wrong-password",
                        "csrf_token": "x" * 32,
                    },
                    headers=headers,
                )
                assert response.status_code in {400, 401, 429}

    source_key = build_source_rate_limit_key(CLIENT_A)
    assert source_key in store.rows


@pytest.mark.integration
def test_uvicorn_rotating_spoofed_leftmost_does_not_create_new_buckets(
    uvicorn_server: str,
) -> None:
    store = _MemoryRateLimitStore()
    with _patched_rate_limiter(store):
        with httpx.Client(base_url=uvicorn_server, timeout=2.0) as client:
            for index in range(4):
                headers = {
                    "X-Forwarded-For": f"203.0.113.{index}, {CLIENT_A}, {LOCAL_TRUSTED_PEER}"
                }
                response = client.post(
                    "/admin/login",
                    data={
                        "username": "ghost",
                        "password": "wrong-password",
                        "csrf_token": "y" * 32,
                    },
                    headers=headers,
                )
                if index < 2:
                    assert response.status_code in {400, 401}
                else:
                    assert response.status_code == 429

    assert len(store.rows) == 1
    assert build_source_rate_limit_key(CLIENT_A) in store.rows
    assert build_source_rate_limit_key(SPOOFED) not in store.rows
