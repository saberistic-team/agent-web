from __future__ import annotations

from review_decision import is_fixable_changes_requested, resolve_decision


def test_fixable_includes_coverage_and_visual() -> None:
    body = (
        "### reviewer_decision\n"
        "- decision: `changes-requested`\n"
        "- hard_fails:\n"
        "  - service coverage below required thresholds\n"
        "  - visual readability: text overflows mobile viewport (out of frame)\n"
    )
    assert is_fixable_changes_requested(body)


def test_fixable_includes_empty_preview_data() -> None:
    body = (
        "### reviewer_decision\n"
        "- decision: `changes-requested`\n"
        "- hard_fails:\n"
        "  - admin preview empty data: screenshot page(s) rendered without mock rows — "
        "`/admin/briefs` reason=`empty_table`\n"
    )
    assert is_fixable_changes_requested(body)
    assert (
        resolve_decision(
            latest_state="CHANGES_REQUESTED",
            latest_body=body,
            prior_changes_requested=3,
        )
        == "changes-requested"
    )


def test_fixable_includes_invisible_desktop_admin_nav() -> None:
    body = (
        "### reviewer_decision\n"
        "- decision: `changes-requested`\n"
        "- hard_fails:\n"
        "  - admin desktop nav invisible: screenshot page(s) have `.admin-nav-link` in "
        "DOM but none visible — `/admin` reason=`desktop_nav_invisible`; "
        "builder must override UA closed-`details` display on desktop\n"
    )
    assert is_fixable_changes_requested(body)
    assert (
        resolve_decision(
            latest_state="CHANGES_REQUESTED",
            latest_body=body,
            prior_changes_requested=3,
        )
        == "changes-requested"
    )


def test_fixable_includes_merge_conflicts() -> None:
    body = (
        "### reviewer_decision\n"
        "- decision: `changes-requested`\n"
        "- hard_fails:\n"
        "  - PR has merge conflicts with base "
        "(mergeable=`False`, mergeable_state=`dirty`) — "
        "return to Builder to resolve on the same PR head\n"
    )
    assert is_fixable_changes_requested(body)
    assert (
        resolve_decision(
            latest_state="CHANGES_REQUESTED",
            latest_body=body,
            prior_changes_requested=5,
        )
        == "changes-requested"
    )


def test_fixable_includes_docs_stub_gaps() -> None:
    body = (
        "### reviewer_decision\n"
        "- hard_fails:\n"
        "  - docs PR is agent-updates stub only — required deliverable "
        "files missing; return to Docs\n"
    )
    assert is_fixable_changes_requested(body)
    assert (
        resolve_decision(
            latest_state="CHANGES_REQUESTED",
            latest_body=body,
            prior_changes_requested=4,
        )
        == "changes-requested"
    )


def test_terminal_worklog_not_fixable() -> None:
    body = "PR is builder worklog-only (terminal: true — do not requeue builder)"
    assert not is_fixable_changes_requested(body)


def test_resolve_keeps_requeueing_fixable_after_many_reviews() -> None:
    """Coverage/visual must not become status:blocked on the 2nd CHANGES_REQUESTED."""
    body = (
        "### reviewer_decision\n"
        "- hard_fails:\n"
        "  - service coverage below required thresholds (unit ≥90%)\n"
    )
    assert (
        resolve_decision(
            latest_state="CHANGES_REQUESTED",
            latest_body=body,
            prior_changes_requested=5,
        )
        == "changes-requested"
    )


def test_resolve_blocks_terminal_marker() -> None:
    assert (
        resolve_decision(
            latest_state="CHANGES_REQUESTED",
            latest_body="worklog-only (terminal: true)",
            prior_changes_requested=1,
        )
        == "blocked"
    )


def test_resolve_blocks_judgment_ping_pong() -> None:
    body = "### reviewer_decision\n- hard_fails:\n  - product judgment: wrong positioning\n"
    assert (
        resolve_decision(
            latest_state="CHANGES_REQUESTED",
            latest_body=body,
            prior_changes_requested=2,
        )
        == "blocked"
    )


def test_resolve_approved() -> None:
    assert (
        resolve_decision(
            latest_state="APPROVED",
            latest_body="ok",
            prior_changes_requested=0,
        )
        == "approved"
    )


def test_latest_submitted_review_retries_until_present(monkeypatch) -> None:
    from review_decision import latest_submitted_review

    calls = {"n": 0}

    def fake_api(method, path, **kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            return []
        return [
            {"state": "COMMENTED", "body": "note"},
            {"state": "CHANGES_REQUESTED", "body": "fix me", "id": 9},
        ]

    monkeypatch.setattr("review_decision.api", fake_api)
    monkeypatch.setattr("review_decision.time.sleep", lambda *_: None)
    review = latest_submitted_review("o/r", 1, attempts=5, delay_sec=0)
    assert review is not None
    assert review["id"] == 9
    assert calls["n"] == 3
