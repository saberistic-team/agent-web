"""Shared pytest defaults for admin security env vars."""

from __future__ import annotations

import pytest

TEST_LIMITER_SECRET = "test-login-limiter-secret-32chars-min"


@pytest.fixture(autouse=True)
def _default_admin_login_limiter_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
