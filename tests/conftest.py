"""Shared pytest configuration for admin security env defaults."""

from __future__ import annotations

import os

TEST_LIMITER_SECRET = "test-limiter-secret-32chars-minimum!!"
TEST_LIMITER_SECRET_ALT = "alt-limiter-secret-32chars-minimum-x!"


def pytest_configure(config) -> None:
    """Set limiter secrets before test modules import TestClient (lifespan)."""
    os.environ.setdefault("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
