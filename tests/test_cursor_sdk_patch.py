"""Unit tests for Cursor SDK bridge auth-token workaround."""

from __future__ import annotations

import pytest

from cursor_sdk_patch import safe_auth_token


@pytest.mark.unit
def test_safe_auth_token_never_starts_with_hyphen() -> None:
    for _ in range(200):
        token = safe_auth_token(8)
        assert token
        assert not token.startswith("-")


@pytest.mark.unit
def test_tool_callback_auth_token_is_retryable() -> None:
    from run_agent import is_retryable_codegen_failure

    err = RuntimeError(
        "Cursor SDK local startup failed (retryable=False): "
        "Bridge exited before discovery with status 1: cursor-sdk-bridge failed: "
        "Error: Missing value for --tool-callback-auth-token"
    )
    assert is_retryable_codegen_failure(err)
