"""Unit tests for post-deploy evidence recording (no live API).

Post-deploy visual verification is a manual admin step, not an automated
pass/fail gate — see ``post_deploy_visual.py``'s module docstring.
"""

from __future__ import annotations

from unittest.mock import patch

from post_deploy_visual import (
    notify_deploy,
    open_or_reuse_record_pr,
    record_branch_name,
    record_pr_body,
)


def test_record_branch_name_is_deterministic() -> None:
    assert record_branch_name("abc123def456extra") == "deploy/health-abc123def456"
    assert record_branch_name("") == "deploy/health-local"


def test_record_pr_body_mentions_sha_and_auto_merge() -> None:
    body = record_pr_body("abc123def456", "https://saberistic.com")
    assert "abc123def456" in body
    assert "auto-merge" in body.lower()
    assert "WORKFLOW_GOVERNANCE" in body


def test_open_or_reuse_record_pr_opens_with_auto_merge_when_absent() -> None:
    with (
        patch(
            "post_deploy_visual.find_open_pr_for_branch", return_value=None
        ) as find_pr,
        patch(
            "post_deploy_visual.open_pull_request",
            return_value={"number": 11, "node_id": "PR_kwzz", "html_url": "https://x/11"},
        ) as open_pr,
        patch("post_deploy_visual.enable_auto_merge") as auto_merge,
    ):
        result = open_or_reuse_record_pr(
            "o/r",
            "deploy/health-abc123def456",
            "main",
            short="abc123def456",
            base_url="https://saberistic.com",
        )

    assert result == {"number": 11, "url": "https://x/11"}
    find_pr.assert_called_once_with("o/r", "deploy/health-abc123def456")
    open_pr.assert_called_once_with(
        "o/r",
        head="deploy/health-abc123def456",
        base="main",
        title="deploy: record post-deploy artifacts (abc123def456)",
        body=record_pr_body("abc123def456", "https://saberistic.com"),
    )
    auto_merge.assert_called_once_with("o/r", "PR_kwzz")


def test_open_or_reuse_record_pr_reuses_existing_open_pr() -> None:
    with (
        patch(
            "post_deploy_visual.find_open_pr_for_branch",
            return_value={"number": 12, "html_url": "https://x/12"},
        ),
        patch("post_deploy_visual.open_pull_request") as open_pr,
        patch("post_deploy_visual.enable_auto_merge") as auto_merge,
    ):
        result = open_or_reuse_record_pr(
            "o/r",
            "deploy/health-abc123def456",
            "main",
            short="abc123def456",
            base_url="https://saberistic.com",
        )

    assert result == {"number": 12, "url": "https://x/12"}
    open_pr.assert_not_called()
    auto_merge.assert_not_called()


def test_notify_deploy_always_comments_record_pr() -> None:
    """The record PR's CODEOWNER reviewer must see screenshots inline, even
    when no issue is linked (no `Closes #N` / `(#N)` in the commit/PR)."""
    with patch("post_deploy_visual.post_issue_comment") as comment:
        notify_deploy("o/r", None, 42, "### deploy_record\nbody")
    comment.assert_called_once_with("o/r", 42, "### deploy_record\nbody")


def test_notify_deploy_also_comments_linked_issue() -> None:
    with patch("post_deploy_visual.post_issue_comment") as comment:
        notify_deploy("o/r", 99, 42, "### deploy_visual_check\nbody")
    assert comment.call_args_list == [
        (("o/r", 42, "### deploy_visual_check\nbody"), {}),
        (("o/r", 99, "### deploy_visual_check\nbody"), {}),
    ]
