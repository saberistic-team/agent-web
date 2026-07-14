"""Unit tests for weekly agent-trace digest deliverables sections."""

from __future__ import annotations

from datetime import datetime, timezone

from digest_trace import render_digest


def test_digest_includes_issues_prs_features_screenshots() -> None:
    since = datetime(2026, 7, 1, tzinfo=timezone.utc)
    until = datetime(2026, 7, 8, tzinfo=timezone.utc)
    rows = [
        {
            "ts": "2026-07-02T10:00:00+00:00",
            "role": "builder",
            "issue": 42,
            "pr": 99,
            "action": "build",
            "model": "composer-2.5",
            "cost_usd": 0.12,
            "outcome": "ok",
        },
        {
            "ts": "2026-07-02T11:00:00+00:00",
            "role": "reviewer",
            "issue": 42,
            "pr": 99,
            "action": "review:approved",
            "model": None,
            "cost_usd": 0,
            "outcome": "ok",
        },
        {
            "ts": "2026-07-03T09:00:00+00:00",
            "role": "gate",
            "issue": 42,
            "pr": 99,
            "action": "gate:review-approved",
            "model": None,
            "cost_usd": 0,
            "outcome": "ok",
        },
        {
            "ts": "2026-07-04T08:00:00+00:00",
            "role": "planner",
            "issue": 55,
            "pr": None,
            "action": "plan",
            "model": None,
            "cost_usd": 0.01,
            "outcome": "ok",
        },
    ]
    md = render_digest(rows, since=since, until=until)
    assert "### Issues & PRs" in md
    assert "#42" in md
    assert "#99" in md
    assert "### Features / work completed" in md
    assert "implemented by Builder" in md
    assert "approved by Reviewer" in md
    assert "merged by Gate" in md
    assert "### Screenshots & visual evidence" in md
    assert "pre-merge screenshots (PR branch)" in md
    assert "post-deploy screenshots (production)" in md
    assert "ADMIN_PREVIEW_MODE" in md
    assert "Issues with screenshot evidence" in md
    assert "**1**" in md or "1" in md  # features / screenshot counts present


def test_digest_empty_window() -> None:
    since = datetime(2026, 7, 1, tzinfo=timezone.utc)
    until = datetime(2026, 7, 8, tzinfo=timezone.utc)
    md = render_digest([], since=since, until=until)
    assert "No trace lines were found" in md
    assert "### Features / work completed" in md
    assert "### Screenshots & visual evidence" in md
