from __future__ import annotations

from acceptance import mark_body_checkboxes, parse_criteria, verify_acceptance
from github_api import GitHubError


def test_parse_criteria_section() -> None:
    body = """## Goal
Something

## Acceptance criteria
- [ ] First thing
- [ ] Second thing with link https://example.com
- Third without box

## Out of scope
- Ignore me
"""
    items = parse_criteria(body)
    assert items == [
        "First thing",
        "Second thing with link https://example.com",
        "Third without box",
    ]


def test_mark_body_checkboxes() -> None:
    body = "## Acceptance criteria\n- [ ] First thing\n- [ ] Second thing\n"
    updated = mark_body_checkboxes(body, ["First thing"])
    assert "- [x] First thing" in updated
    assert "- [ ] Second thing" in updated


def test_verify_acceptance_ai_error_is_not_product_not_done(monkeypatch) -> None:
    """Cursor prose instead of JSON must not invent not_done rows (anti-loop)."""

    def fake_gather(repo, issue, pr_number=None):
        return {
            "issue_body": (
                "## Acceptance criteria\n"
                "- [ ] Supported stages are X\n"
                "- [ ] Tests cover transitions\n"
            ),
            "issue_title": "pipeline",
            "issue_url": "https://example.com/i/1",
            "pr_url": "https://example.com/p/1",
            "head_sha": "abc",
            "commits": [],
            "files": [],
            "comments": [],
            "deploy": {},
        }

    def boom(*_a, **_k):
        raise GitHubError(
            "acceptance AI did not return JSON: "
            "'Gathering PR and codebase evidence against each acceptance criterion.'"
        )

    monkeypatch.setattr("acceptance.gather_evidence", fake_gather)
    monkeypatch.setattr("acceptance.ai_check_remaining", boom)

    result = verify_acceptance("o/r", 1, 1, use_ai=True)
    assert result["ai_infra_failed"] is True
    assert all(i["method"] == "ai-error" for i in result["items"])
    assert all(i["status"] == "n/a" for i in result["items"])
    assert result["all_done"] is False
