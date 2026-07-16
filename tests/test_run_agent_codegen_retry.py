"""Builder codegen failures must requeue / reuse open PRs (not false blocked)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from run_agent import (
    is_github_app_write_permission_failure,
    is_retryable_codegen_failure,
    recover_builder_after_codegen_failure,
)


@pytest.mark.unit
@pytest.mark.parametrize(
    "message",
    [
        "Cursor SDK local startup failed (retryable=True): Bridge request timed out: ReadTimeout: timed out",
        "Cursor SDK local startup failed (retryable=False): Bridge exited before discovery with status 1: Missing value for --tool-callback-auth-token",
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
        'POST /repos/o/r/git/trees -> 403: {"message":"Resource not accessible by integration"}',
    ],
)
def test_non_retryable_codegen_failures(message: str) -> None:
    assert not is_retryable_codegen_failure(RuntimeError(message))


@pytest.mark.unit
@pytest.mark.parametrize(
    "message",
    [
        'POST /repos/saberistic-team/agent-web/git/trees -> 403: {"message":"Resource not accessible by integration","documentation_url":"https://docs.github.com/rest/git/trees#create-a-tree","status":"403"}',
        "PUT /repos/o/r/contents/app/x.py -> 403 Resource not accessible by integration",
        "git/refs 403 create ref denied",
    ],
)
def test_github_app_write_permission_failures(message: str) -> None:
    assert is_github_app_write_permission_failure(RuntimeError(message))


@pytest.mark.unit
def test_recover_codegen_failure_handoffs_existing_pr_instead_of_blocking() -> None:
    exc = RuntimeError(
        'POST /repos/o/r/git/trees -> 403: {"message":"Resource not accessible by integration"}'
    )
    with (
        patch("run_agent.linked_open_prs", return_value=[{"number": 218}]),
        patch("run_agent.handoff_builder_when_mergeable") as handoff,
        patch("run_agent.post_issue_comment") as comment,
        patch("run_agent.escalate") as escalate,
        patch("run_agent.write_builder_handoff") as write_handoff,
    ):
        mode = recover_builder_after_codegen_failure(
            "o/r",
            210,
            exc,
            detail="Codegen failed",
        )
    assert mode == "reviewer"
    handoff.assert_called_once_with("o/r", 210)
    escalate.assert_not_called()
    write_handoff.assert_not_called()
    assert "builder_codegen_existing_pr" in comment.call_args.args[2]


@pytest.mark.unit
def test_recover_codegen_failure_blocks_when_no_pr() -> None:
    exc = RuntimeError(
        'POST /repos/o/r/git/trees -> 403: {"message":"Resource not accessible by integration"}'
    )
    with (
        patch("run_agent.linked_open_prs", return_value=[]),
        patch("run_agent.handoff_builder_when_mergeable") as handoff,
        patch("run_agent.post_issue_comment"),
        patch("run_agent.escalate") as escalate,
        patch("run_agent.write_builder_handoff") as write_handoff,
    ):
        mode = recover_builder_after_codegen_failure(
            "o/r",
            210,
            exc,
            detail="Codegen failed",
        )
    assert mode == "blocked"
    handoff.assert_not_called()
    escalate.assert_called_once()
    write_handoff.assert_called_once_with("blocked")
