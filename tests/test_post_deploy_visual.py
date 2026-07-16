"""Unit tests for post-deploy visual provider selection (no live API)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from github_api import GitHubError

from post_deploy_visual import (
    _parse_visual_json,
    open_or_reuse_record_pr,
    record_branch_name,
    record_pr_body,
    visual_ai_check,
)


def test_parse_visual_json_fenced() -> None:
    raw = '```json\n{"visible": true, "summary": "ok", "decision": "pass"}\n```'
    data = _parse_visual_json(raw, model="m", provider="cursor")
    assert data["decision"] == "pass"
    assert data["visible"] is True
    assert data["provider"] == "cursor"


def test_visual_ai_check_skips_without_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("VISUAL_PROVIDER", raising=False)
    result = visual_ai_check(
        issue_title="t",
        issue_body="b",
        pre_paths=[],
        post_paths=[],
    )
    assert result["decision"] == "skip"
    assert result["provider"] == "none"


def test_visual_ai_check_prefers_cursor(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CURSOR_API_KEY", "ck_test")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("VISUAL_PROVIDER", raising=False)
    post = tmp_path / "post-home.png"
    post.write_bytes(b"png")
    with patch(
        "post_deploy_visual.visual_ai_check_cursor",
        return_value={
            "visible": True,
            "summary": "seen",
            "decision": "pass",
            "model": "composer-2.5",
            "provider": "cursor",
        },
    ) as cursor:
        with patch("post_deploy_visual.visual_ai_check_openai") as openai:
            result = visual_ai_check(
                issue_title="Email-only form",
                issue_body="Remove phone",
                pre_paths=[],
                post_paths=[post],
            )
    assert result["provider"] == "cursor"
    assert result["decision"] == "pass"
    cursor.assert_called_once()
    openai.assert_not_called()


def test_visual_ai_check_falls_back_to_openai(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CURSOR_API_KEY", "ck_test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk_test")
    monkeypatch.delenv("VISUAL_PROVIDER", raising=False)
    post = tmp_path / "post-home.png"
    post.write_bytes(b"png")
    with patch(
        "post_deploy_visual.visual_ai_check_cursor",
        side_effect=GitHubError("cursor down"),
    ):
        with patch(
            "post_deploy_visual.visual_ai_check_openai",
            return_value={
                "visible": True,
                "summary": "backup",
                "decision": "pass",
                "model": "gpt-4o-mini",
                "provider": "openai",
            },
        ) as openai:
            result = visual_ai_check(
                issue_title="t",
                issue_body="b",
                pre_paths=[],
                post_paths=[post],
            )
    assert result["provider"] == "openai"
    openai.assert_called_once()


def test_visual_ai_check_force_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CURSOR_API_KEY", "ck_test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk_test")
    monkeypatch.setenv("VISUAL_PROVIDER", "openai")
    with patch("post_deploy_visual.visual_ai_check_cursor") as cursor:
        with patch(
            "post_deploy_visual.visual_ai_check_openai",
            return_value={
                "visible": False,
                "summary": "forced",
                "decision": "fail",
                "model": "gpt",
                "provider": "openai",
            },
        ):
            result = visual_ai_check(
                issue_title="t",
                issue_body="b",
                pre_paths=[],
                post_paths=[],
            )
    assert result["provider"] == "openai"
    cursor.assert_not_called()


def test_record_branch_name_is_deterministic() -> None:
    assert record_branch_name("abc123def456extra") == "deploy/screenshots-abc123def456"
    assert record_branch_name("") == "deploy/screenshots-local"


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
            "deploy/screenshots-abc123def456",
            "main",
            short="abc123def456",
            base_url="https://saberistic.com",
        )

    assert result == {"number": 11, "url": "https://x/11"}
    find_pr.assert_called_once_with("o/r", "deploy/screenshots-abc123def456")
    open_pr.assert_called_once_with(
        "o/r",
        head="deploy/screenshots-abc123def456",
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
            "deploy/screenshots-abc123def456",
            "main",
            short="abc123def456",
            base_url="https://saberistic.com",
        )

    assert result == {"number": 12, "url": "https://x/12"}
    open_pr.assert_not_called()
    auto_merge.assert_not_called()
