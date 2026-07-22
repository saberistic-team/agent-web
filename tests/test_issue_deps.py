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
    monkeypatch.setattr(
        "issue_deps.fetch_issue_relationships",
        lambda *_a, **_k: {"subIssues": {"nodes": []}},
    )
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
    monkeypatch.setattr(
        "issue_deps.fetch_issue_relationships",
        lambda *_a, **_k: {"subIssues": {"nodes": []}},
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
        "issue_deps.fetch_issue_relationships",
        lambda *_a, **_k: {"subIssues": {"nodes": []}},
    )
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
        "issue_deps.fetch_issue_relationships",
        lambda *_a, **_k: {"subIssues": {"nodes": []}},
    )
    monkeypatch.setattr(
        "issue_deps._issue_state",
        lambda *_a, **_k: {"number": 199, "title": "Manifest", "state": "closed"},
    )
    require_dependencies_met("o/r", 204, body="Depends on: #199\n")


@pytest.mark.unit
def test_infer_dependency_from_prose() -> None:
    from issue_deps import infer_dependency_issue_numbers

    body = (
        "Start only after completing #199. Schema produced by #200. "
        "#201 must land first before this spike.\n"
    )
    assert infer_dependency_issue_numbers(body) == [199, 200, 201]


@pytest.mark.unit
def test_open_sub_issues_block_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("issue_deps.fetch_github_blocked_by", lambda *_a, **_k: [])
    monkeypatch.setattr(
        "issue_deps.fetch_issue_relationships",
        lambda *_a, **_k: {
            "subIssues": {
                "nodes": [
                    {"number": 401, "title": "Child A", "state": "OPEN"},
                    {"number": 402, "title": "Child B", "state": "CLOSED"},
                ]
            }
        },
    )
    blockers = open_dependency_blockers("o/r", 400, body="")
    assert [b["number"] for b in blockers] == [401]
    assert blockers[0]["source"] == "open_sub_issue"


@pytest.mark.unit
def test_reconcile_adds_missing_blocked_by(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from issue_deps import reconcile_issue_dependencies

    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "issue_deps.fetch_issue_relationships",
        lambda *_a, **_k: {
            "id": "I_parent",
            "number": 204,
            "body": "Depends on: #199\n",
            "parent": None,
            "blockedBy": {"nodes": []},
            "blocking": {"nodes": []},
            "subIssues": {"nodes": []},
        },
    )
    monkeypatch.setattr(
        "issue_deps._issue_state",
        lambda *_a, **_k: {
            "number": 199,
            "title": "Manifest",
            "state": "open",
            "id": "I_199",
        },
    )
    monkeypatch.setattr(
        "issue_deps.add_blocked_by_link",
        lambda repo, issue, blocking: (
            calls.append((issue, blocking)) or "ok"
        ),
    )
    monkeypatch.setattr("issue_deps._patch_issue_body", lambda *_a, **_k: None)
    monkeypatch.setattr("issue_deps._collect_pr_inferred_deps", lambda *_a, **_k: [])
    monkeypatch.setattr(
        "issue_deps._parent_from_planner_comments", lambda *_a, **_k: None
    )

    summary = reconcile_issue_dependencies("o/r", 204, write=True)
    assert calls == [(204, 199)]
    assert summary["added_blocked_by"][0]["blocking"] == 199
    assert [b["number"] for b in summary["blockers"]] == [199]
    # Body already had Depends on: #199 — no rewrite needed.
    assert summary["body_updated"] is False


@pytest.mark.unit
def test_reconcile_syncs_body_when_missing_depends_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from issue_deps import reconcile_issue_dependencies

    patched: list[str] = []
    monkeypatch.setattr(
        "issue_deps.fetch_issue_relationships",
        lambda *_a, **_k: {
            "id": "I_parent",
            "number": 204,
            "body": (
                "## Dependencies\n"
                "Start only after completing #199.\n"
            ),
            "parent": None,
            "blockedBy": {"nodes": []},
            "blocking": {"nodes": []},
            "subIssues": {"nodes": []},
        },
    )
    monkeypatch.setattr(
        "issue_deps._issue_state",
        lambda *_a, **_k: {
            "number": 199,
            "title": "Manifest",
            "state": "open",
            "id": "I_199",
        },
    )
    monkeypatch.setattr(
        "issue_deps.add_blocked_by_link",
        lambda *_a, **_k: "ok",
    )
    monkeypatch.setattr(
        "issue_deps._patch_issue_body",
        lambda repo, issue, body: patched.append(body),
    )
    monkeypatch.setattr("issue_deps._collect_pr_inferred_deps", lambda *_a, **_k: [])
    monkeypatch.setattr(
        "issue_deps._parent_from_planner_comments", lambda *_a, **_k: None
    )

    summary = reconcile_issue_dependencies("o/r", 204, write=True)
    assert summary["body_updated"] is True
    assert patched and "Depends on: #199" in patched[0]
