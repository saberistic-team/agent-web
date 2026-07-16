"""Shared env wiring for ADMIN_PREVIEW_MODE route tests."""

from __future__ import annotations

import pytest


def apply_admin_preview_env(
    monkeypatch: pytest.MonkeyPatch,
    *,
    base_url: str = "http://127.0.0.1:8765",
    app_env: str = "preview",
    bind_host: str = "127.0.0.1",
    seed: str | None = None,
) -> None:
    """Set the minimum env vars for a validated local preview bypass."""
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("APP_ENV", app_env)
    monkeypatch.setenv("BASE_URL", base_url)
    monkeypatch.setenv("SERVER_BIND_HOST", bind_host)
    if seed is not None:
        monkeypatch.setenv("ADMIN_PREVIEW_SEED", seed)
