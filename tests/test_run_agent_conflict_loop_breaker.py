"""Repeated identical merge-conflict smoke failures must escalate, not loop forever.

Regression coverage for issue #242: Builder's own codegen reproduced the same
`ImportError` (a symbol split between `app/admin_security.py` and
`app/admin_secrets.py`) across 54+ consecutive `broken_after_resolve` cycles
over 7+ hours because nothing counted repeats of an identical smoke error.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from run_agent import (
    REPEATED_CONFLICT_FAILURE_LIMIT,
    handoff_builder_when_mergeable,
    repeated_conflict_smoke_signature,
)

IMPORT_ERROR = (
    "pytest collect failed: ImportError: cannot import name "
    "'validate_admin_security_config' from 'app.admin_security'"
)


def _conflict_result_comment(status: str, smoke_error: str | None = None) -> dict:
    lines = [
        "### builder_conflict_result",
        f"- status: `{status}`",
        "- pr: #255",
    ]
    if smoke_error is not None:
        lines.append(f"- smoke_error: `{smoke_error}`")
    return {"body": "\n".join(lines)}


def _generic_followup(status: str) -> dict:
    return {
        "body": (
            "### builder_conflict_result\n"
            f"- status: `{status}`\n"
            "- note: conflict resolution did not finish cleanly; "
            "re-entering `status:queued` (not handing off to Reviewer)."
        )
    }


@pytest.mark.unit
def test_identical_smoke_error_repeated_returns_signature() -> None:
    comments = []
    for _ in range(REPEATED_CONFLICT_FAILURE_LIMIT):
        comments.append(_conflict_result_comment("broken_after_resolve", IMPORT_ERROR))
        comments.append(_generic_followup("broken_after_resolve"))
    with patch("run_agent.list_issue_comments", return_value=comments):
        signature = repeated_conflict_smoke_signature("o/r", 242)
    assert signature is not None
    assert "validate_admin_security_config" in signature


@pytest.mark.unit
def test_below_threshold_does_not_trigger() -> None:
    comments = [
        _conflict_result_comment("broken_after_resolve", IMPORT_ERROR),
        _generic_followup("broken_after_resolve"),
    ]
    with patch("run_agent.list_issue_comments", return_value=comments):
        assert repeated_conflict_smoke_signature("o/r", 242) is None


@pytest.mark.unit
def test_changing_smoke_error_does_not_trigger() -> None:
    comments = []
    for i in range(REPEATED_CONFLICT_FAILURE_LIMIT):
        comments.append(
            _conflict_result_comment("broken_after_resolve", f"different error #{i}")
        )
        comments.append(_generic_followup("broken_after_resolve"))
    with patch("run_agent.list_issue_comments", return_value=comments):
        assert repeated_conflict_smoke_signature("o/r", 242) is None


@pytest.mark.unit
def test_progress_more_recent_than_old_failures_resets_streak() -> None:
    # ``list_issue_comments`` returns oldest-first, matching the GitHub API.
    # A batch of old failures followed by a *more recent* successful merge
    # must not be treated as an ongoing streak.
    comments = []
    for _ in range(REPEATED_CONFLICT_FAILURE_LIMIT):
        comments.append(_conflict_result_comment("broken_after_resolve", IMPORT_ERROR))
        comments.append(_generic_followup("broken_after_resolve"))
    comments.append(_conflict_result_comment("resolved"))
    with patch("run_agent.list_issue_comments", return_value=comments):
        assert repeated_conflict_smoke_signature("o/r", 242) is None


@pytest.mark.unit
def test_handoff_escalates_instead_of_looping_when_signature_repeats() -> None:
    with (
        patch(
            "builder_conflicts.maybe_resolve_pr_conflicts",
            return_value={"status": "broken_after_resolve", "pr": 255},
        ),
        patch("run_agent.post_issue_comment"),
        patch(
            "run_agent.repeated_conflict_smoke_signature",
            return_value=IMPORT_ERROR,
        ),
        patch("run_agent.escalate") as escalate,
        patch("run_agent.write_builder_handoff") as write_handoff,
    ):
        handoff_builder_when_mergeable("o/r", 242)
    escalate.assert_called_once()
    assert "255" in escalate.call_args.args[2]
    write_handoff.assert_called_once_with("blocked")


@pytest.mark.unit
def test_handoff_keeps_retrying_when_signature_not_yet_repeated() -> None:
    with (
        patch(
            "builder_conflicts.maybe_resolve_pr_conflicts",
            return_value={"status": "broken_after_resolve", "pr": 255},
        ),
        patch("run_agent.post_issue_comment"),
        patch("run_agent.repeated_conflict_smoke_signature", return_value=None),
        patch("run_agent.escalate") as escalate,
        patch("run_agent.write_builder_handoff") as write_handoff,
    ):
        handoff_builder_when_mergeable("o/r", 242)
    escalate.assert_not_called()
    write_handoff.assert_called_once_with("waiting")
