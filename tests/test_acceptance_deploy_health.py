"""Acceptance close must require post-merge deploy health for CRM runtime issues (#280)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from acceptance import close_issue_if_accepted


@pytest.mark.unit
def test_close_issue_if_accepted_requires_deploy_health_for_crm_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with patch("acceptance.require_checklist_complete", return_value={"url": "http://check"}):
        with patch(
            "crm_deploy_health.require_post_merge_deploy_health",
            side_effect=RuntimeError("missing deploy health"),
        ) as gate:
            with pytest.raises(RuntimeError, match="missing deploy health"):
                close_issue_if_accepted(
                    "o/r",
                    230,
                    merge_sha="sha230",
                    pr_number=250,
                )
            gate.assert_called_once_with("o/r", 230, "sha230", pr_number=250)


@pytest.mark.unit
def test_close_issue_if_accepted_posts_gate_comment_when_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    comments: list[str] = []

    def capture_comment(repo: str, issue: int, body: str) -> dict:  # noqa: ARG001
        comments.append(body)
        return {"html_url": f"https://example.com/{issue}"}

    with patch("acceptance.require_checklist_complete", return_value={"url": "http://check"}):
        with patch(
            "crm_deploy_health.require_post_merge_deploy_health",
            return_value={
                "required": True,
                "path": ".agent/deploy/sha230/deploy-health.json",
                "record": {
                    "verification_layers": {"post_deploy_functional_health": "pass"},
                },
            },
        ):
            with patch("acceptance.api", return_value={"body": ""}):
                with patch("acceptance.post_issue_comment", side_effect=capture_comment):
                    close_issue_if_accepted(
                        "o/r",
                        230,
                        merge_sha="sha230",
                        pr_number=250,
                    )
    assert any("deploy_health_gate" in body for body in comments)
