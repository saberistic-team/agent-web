#!/usr/bin/env python3
"""Workarounds for known Cursor Python SDK bridge bugs.

The local bridge rejects callback auth tokens that start with ``-`` (arg parse
treats them as flags), which surfaces as::

    Missing value for --tool-callback-auth-token

SDK marks that as ``retryable=False``, which previously escalated Builder to
``status:blocked``. Patch token minting so tokens never start with ``-``, and
treat residual bridge/token failures as dispatcher-retriable.
"""

from __future__ import annotations

import secrets


def safe_auth_token(nbytes: int = 32) -> str:
    """``token_urlsafe`` that never starts with ``-`` (bridge argv safe)."""
    while True:
        token = secrets.token_urlsafe(nbytes)
        if not token.startswith("-"):
            return token


def patch_callback_auth_tokens() -> bool:
    """Monkeypatch SDK token minting. Returns True when a patch was applied."""
    applied = False
    for mod_name in ("cursor_sdk._tool_callback", "cursor_sdk._store_callback"):
        try:
            mod = __import__(mod_name, fromlist=["_new_auth_token"])
        except Exception:
            continue
        if not hasattr(mod, "_new_auth_token"):
            continue

        def _token() -> str:
            return safe_auth_token()

        setattr(mod, "_new_auth_token", _token)
        applied = True
    return applied
