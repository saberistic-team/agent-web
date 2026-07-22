"""Repeated identical *pre-handoff* smoke failures must escalate, not loop forever.

Regression coverage for issue #115 / PR #265: `handoff_builder_when_mergeable`'s
direct `smoke_pr_head` call (mergeable/clean PR heads — no conflict to
resolve) had no repeat counter at all, unlike the conflict-resolution smoke
path (#242's `repeated_conflict_smoke_signature`). Three consecutive Builder
dispatches reproduced the identical `tests/test_codegen_provider.py` failure
(caused by Builder's own `CURSOR_MODEL` runtime override leaking into the
smoke subprocess — fixed separately in `builder_conflicts._smoke_env`) with
nothing counting the repeats, so Builder just requeued forever.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from run_agent import (
    REPEATED_CONFLICT_FAILURE_LIMIT,
    handoff_builder_when_mergeable,
    repeated_smoke_result_signature,
)

SMOKE_ERROR = (
    "pytest failed: onError: assert 'sonnet' in 'composer-2.5'\n"
    "tests/test_codegen_provider.py:130: AssertionError"
)


def _smoke_result_comment(status: str, smoke_error: str | None = None) -> dict:
    lines = [
        "### builder_smoke_result",
        f"- status: `{status}`",
        "- pr: #265",
    ]
    if smoke_error is not None:
        lines.append(f"- smoke_error: `{smoke_error}`")
    return {"body": "\n".join(lines)}


@pytest.mark.unit
def test_identical_smoke_error_repeated_returns_signature() -> None:
    comments = [
        _smoke_result_comment("smoke_failed", SMOKE_ERROR)
        for _ in range(REPEATED_CONFLICT_FAILURE_LIMIT)
    ]
    with patch("run_agent.list_issue_comments", return_value=comments):
        signature = repeated_smoke_result_signature("o/r", 115)
    assert signature is not None
    assert "sonnet" in signature or "composer" in signature


@pytest.mark.unit
def test_below_threshold_does_not_trigger() -> None:
    comments = [_smoke_result_comment("smoke_failed", SMOKE_ERROR)]
    with patch("run_agent.list_issue_comments", return_value=comments):
        assert repeated_smoke_result_signature("o/r", 115) is None


@pytest.mark.unit
def test_changing_smoke_error_does_not_trigger() -> None:
    comments = [
        _smoke_result_comment("smoke_failed", f"different error #{i}")
        for i in range(REPEATED_CONFLICT_FAILURE_LIMIT)
    ]
    with patch("run_agent.list_issue_comments", return_value=comments):
        assert repeated_smoke_result_signature("o/r", 115) is None


@pytest.mark.unit
def test_progress_more_recent_than_old_failures_resets_streak() -> None:
    # ``list_issue_comments`` returns oldest-first, matching the GitHub API.
    comments = [
        _smoke_result_comment("smoke_failed", SMOKE_ERROR)
        for _ in range(REPEATED_CONFLICT_FAILURE_LIMIT)
    ]
    comments.append(_smoke_result_comment("smoke_repaired"))
    with patch("run_agent.list_issue_comments", return_value=comments):
        assert repeated_smoke_result_signature("o/r", 115) is None


def _patch_clean_mergeable_pr(smoke_result: dict):
    pr = {"number": 265, "head": {"ref": "builder/115-record-authoritative"}}
    return (
        patch(
            "builder_conflicts.maybe_resolve_pr_conflicts",
            return_value={"status": "no_pr"},
        ),
        patch(
            "builder_conflicts.linked_pr_conflict_status",
            return_value={"status": "clean"},
        ),
        patch("builder_conflicts.linked_open_prs", return_value=[pr]),
        patch("builder_conflicts.refresh_pr", return_value=pr),
        patch("builder_conflicts.smoke_pr_head", return_value=smoke_result),
    )


@pytest.mark.unit
def test_handoff_escalates_instead_of_looping_when_signature_repeats() -> None:
    smoke_result = {
        "status": "smoke_failed",
        "pr": 265,
        "smoke_error": SMOKE_ERROR,
        "repairs": [],
    }
    patches = _patch_clean_mergeable_pr(smoke_result)
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patch("run_agent.post_issue_comment"),
        patch(
            "run_agent.repeated_smoke_result_signature",
            return_value=SMOKE_ERROR,
        ),
        patch("run_agent.escalate") as escalate,
        patch("run_agent.write_builder_handoff") as write_handoff,
    ):
        handoff_builder_when_mergeable("o/r", 115)
    escalate.assert_called_once()
    assert "265" in escalate.call_args.args[2]
    write_handoff.assert_called_once_with("blocked")


@pytest.mark.unit
def test_handoff_keeps_retrying_when_signature_not_yet_repeated() -> None:
    smoke_result = {
        "status": "smoke_failed",
        "pr": 265,
        "smoke_error": SMOKE_ERROR,
        "repairs": [],
    }
    patches = _patch_clean_mergeable_pr(smoke_result)
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patch("run_agent.post_issue_comment"),
        patch("run_agent.repeated_smoke_result_signature", return_value=None),
        patch("run_agent.escalate") as escalate,
        patch("run_agent.write_builder_handoff") as write_handoff,
    ):
        handoff_builder_when_mergeable("o/r", 115)
    escalate.assert_not_called()
    write_handoff.assert_called_once_with("waiting")
