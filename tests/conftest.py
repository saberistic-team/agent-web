"""Shared pytest fixtures for the test suite."""

from __future__ import annotations

import pytest
from argon2 import PasswordHasher

from app import admin_auth

TEST_LIMITER_SECRET = "test-limiter-secret-32chars-minimum!!"
TEST_ADMIN_USERNAME = "operator"
TEST_ADMIN_PASSWORD = "correct-horse-battery-staple"
TEST_ADMIN_PASSWORD_HASH = PasswordHasher().hash(TEST_ADMIN_PASSWORD)
TEST_ADMIN_SESSION_SECRET = "test-session-secret-32chars-minimum"


@pytest.fixture(autouse=True)
def admin_login_limiter_secret_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Baseline admin auth/limiter env so startup validation and login routes work."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_ADMIN_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_ADMIN_PASSWORD_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_ADMIN_SESSION_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.delenv("ADMIN_LOGIN_LIMITER_PREVIOUS_SECRET", raising=False)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "5")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_WINDOW_SECONDS", "900")
    monkeypatch.setenv("ADMIN_LOGIN_LOCKOUT_SECONDS", "900")
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    admin_auth.reset_login_rate_limiter()
