"""Shared pytest defaults for admin login limiter secrets."""

from __future__ import annotations

import pytest

TEST_LIMITER_SECRET = "ci-login-limiter-secret-32chars-min!!"
TEST_LIMITER_SECRET_PREVIOUS = "ci-login-limiter-previous-32chars-min!"


@pytest.fixture(autouse=True)
def _default_admin_login_limiter_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.delenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", raising=False)
