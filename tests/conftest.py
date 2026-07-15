"""Shared pytest hooks for admin authentication test environment."""

from __future__ import annotations

import pytest

from app import admin_auth
from tests.test_admin_auth import (
    FakeRateLimitStore,
    TEST_HASH,
    TEST_SECRET,
    TEST_USERNAME,
    _login_flows,
    _session_store,
    client as admin_client,
)

TEST_LIMITER_SECRET = "test-login-limiter-secret-32chars-min!!"


@pytest.fixture(autouse=True)
def _admin_test_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "5")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_WINDOW_SECONDS", "900")
    monkeypatch.setenv("ADMIN_LOGIN_LOCKOUT_SECONDS", "900")
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    admin_auth.reset_login_rate_limiter()
    _login_flows.clear()
    _session_store.clear()
    admin_client.cookies.clear()


@pytest.fixture(autouse=True)
def _reset_admin_client_cookies() -> None:
    admin_client.cookies.clear()
    yield
    admin_client.cookies.clear()


@pytest.fixture
def rate_limit_store() -> FakeRateLimitStore:
    return FakeRateLimitStore()
