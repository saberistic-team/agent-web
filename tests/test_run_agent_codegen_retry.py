"""Builder codegen failures must requeue when retryable (not status:blocked)."""

from __future__ import annotations

import pytest

from run_agent import is_retryable_codegen_failure


@pytest.mark.unit
@pytest.mark.parametrize(
    "message",
    [
        "Cursor SDK local startup failed (retryable=True): Bridge request timed out: ReadTimeout: timed out",
        "Cursor changed too many files (32 > 30): app/admin_companies.py, app/contacts.py",
        "model proposed too many files (15 > 12)",
        "RemoteProtocolError: Server disconnected",
        "temporarily unavailable",
    ],
)
def test_retryable_codegen_failures(message: str) -> None:
    assert is_retryable_codegen_failure(RuntimeError(message))


@pytest.mark.unit
@pytest.mark.parametrize(
    "message",
    [
        "Cursor SDK local startup failed (retryable=False): permanent auth error",
        "acceptance criteria cannot be met from the issue text",
        "Landing scaffold missing (`site/index.html`)",
    ],
)
def test_non_retryable_codegen_failures(message: str) -> None:
    assert not is_retryable_codegen_failure(RuntimeError(message))
