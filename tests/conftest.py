"""Shared pytest fixtures for admin security environment."""

from __future__ import annotations

import pytest

TEST_LIMITER_SECRET = "test-limiter-secret-32chars-minimum"


@pytest.fixture(autouse=True)
def _admin_login_limiter_secret_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
