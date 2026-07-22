"""Unit tests for issue dependency guards (#204)."""

from __future__ import annotations

import pytest

from github_api import GitHubError
from issue_deps import (
    dependency_block_reason,
    has_unstructured_dependencies,
    open_dependency_blockers,
    parse_dependency_issue_numbers,
    require_dependencies_met,
)


@pytest.mark.unit
def test_parse_depends_on_line() -> None:
    body = "Summary\n\nDepends on: #199, #200\n\n## Acceptance criteria\n- [ ] done\n"
    assert parse_dependency_issue_numbers(body) == [199, 200]


@pytest.mark.unit
def test_parse_blocked_by_line_and_urls() -> None:
    body = (
        "Blocked by: https://github.com/saberistic-team/agent-web/issues/199 "
        "and #200\n"
    )
    assert parse_dependency_issue_numbers(body) == [199, 200]


@pytest.mark.unit
def test_parse_dependencies_section_refs() -> None:
    body = (
        "## Dependencies\n"
        "- Manifest: #199\n"
        "- Corpus: #200\n"
        "\n"
        "## Acceptance criteria\n"
        "- [ ] x\n"
    )
    assert parse_dependency_issue_numbers(body) == [199, 200]


@pytest.mark.unit
def test_parse_none_dependencies() -> None:
    assert parse_dependency_issue_numbers("## Dependencies\nNone\n") == []
    assert parse_dependency_issue_numbers("Depends on: N/A\n") == []
    assert not has_unstructured_dependencies("## Dependencies\nNone\n")
    assert not has_unstructured_dependencies("## Dependencies\nN/A\n")


@pytest.mark.unit
def test_unstructured_dependencies_like_issue_204() -> None:
    body = (
        "## Dependencies\n"
        "\n"
        "Start only after Manifest v0, the research corpus, and the MVP "
        "requirements are stable enough to test. If dispatched earlier, "
        "return the issue to planning rather than guessing the schema.\n"
    )
    assert has_unstructured_dependencies(body)
    assert parse_dependency_issue_numbers(body) == []


@pytest.mark.unit
def test_provisional_exemption_clears_unstructured() -> None:
    body = (
        "## Dependencies\n"
        "Provisional OK — spike may use fixture schema.\n"
    )
    assert not has_unstructured_dependencies(body)


@pytest.mark.unit
def test_open_blockers_from_body_skips_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("issue_deps.fetch_github_blocked_by", lambda *_a, **_k: [])
    states = {
        199: {"number": 199, "title": "Manifest", "state": "open"},
        200: {"number": 200, "title": "Corpus", "state": "closed"},
    }
    monkeypatch.setattr(
        "issue_deps._issue_state",
        lambda repo, number: states[number],
    )
    blockers = open_dependency_blockers(
        "o/r",
        204,
        body="Depends on: #199, #200\n",
    )
    assert [b["number"] for b in blockers] == [199]
    assert blockers[0]["source"] == "body"


@pytest.mark.unit
def test_open_blockers_from_github_blocked_by(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "issue_deps.fetch_github_blocked_by",
        lambda *_a, **_k: [
            {"number": 199, "title": "Manifest", "state": "OPEN"},
            {"number": 198, "title": "Done", "state": "CLOSED"},
        ],
    )
    blockers = open_dependency_blockers("o/r", 204, body="")
    assert [b["number"] for b in blockers] == [199]
    assert blockers[0]["source"] == "blocked_by"


@pytest.mark.unit
def test_dependency_block_reason_unstructured() -> None:
    body = (
        "## Dependencies\n"
        "Start only after Manifest v0 and the research corpus are stable.\n"
    )
    reason = dependency_block_reason("o/r", 204, body=body)
    assert reason is not None
    assert "machine-readable" in reason


@pytest.mark.unit
def test_require_dependencies_met_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("issue_deps.fetch_github_blocked_by", lambda *_a, **_k: [])
    monkeypatch.setattr(
        "issue_deps._issue_state",
        lambda *_a, **_k: {"number": 199, "title": "Manifest", "state": "open"},
    )
    with pytest.raises(GitHubError, match="#199"):
        require_dependencies_met("o/r", 204, body="Depends on: #199\n")


@pytest.mark.unit
def test_require_dependencies_met_ok_when_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("issue_deps.fetch_github_blocked_by", lambda *_a, **_k: [])
    monkeypatch.setattr(
        "issue_deps._issue_state",
        lambda *_a, **_k: {"number": 199, "title": "Manifest", "state": "closed"},
    )
    require_dependencies_met("o/r", 204, body="Depends on: #199\n")
