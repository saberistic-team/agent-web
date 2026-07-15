"""Shared pytest configuration for admin security env defaults."""

from __future__ import annotations

import os

import pytest

TEST_LIMITER_SECRET = "test-limiter-secret-32chars-minimum!!"


def pytest_configure(config: pytest.Config) -> None:
    """Ensure limiter secret exists before module-level TestClient startup."""
    os.environ.setdefault("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)


@pytest.fixture(autouse=True)
def _admin_login_limiter_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.delenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", raising=False)
