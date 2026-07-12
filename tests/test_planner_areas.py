"""Planner change-area extraction (no GitHub API)."""

from __future__ import annotations

from run_agent import (
    child_issue_body,
    extract_acceptance_section,
    plan_change_areas,
)

ISSUE_55_STYLE = """
## Summary

Ship email-on-submit.

## Current behavior (study notes)

Emails only after Stripe pay.

## Desired behavior

Email on submit regardless of payment.

## Out of scope

Admin UI.

## Implementation hints

| Area | Files |
|------|--------|
| Submit | app/main.py |

## Acceptance criteria

- [ ] Email only contact
- [ ] Lead email on submit
"""


def test_plan_change_areas_keeps_narrative_issue_single() -> None:
    assert plan_change_areas(ISSUE_55_STYLE) == []


def test_plan_change_areas_uses_work_packages_bullets() -> None:
    body = """
## Summary

Parent feature.

## Work packages

- Add submit-time Resend lead emails
- Remove phone contact from form/API
- Update PROJECT_BRIEF docs

## Acceptance criteria

- [ ] Done
"""
    areas = plan_change_areas(body)
    assert areas == [
        "Add submit-time Resend lead emails",
        "Remove phone contact from form/API",
        "Update PROJECT_BRIEF docs",
    ]


def test_plan_change_areas_actionable_h2s() -> None:
    body = """
## Summary

Multi-area feature.

## Add Resend lead emails on submit

Wire create_brief.

## Remove phone contact option

UI + API.

## Acceptance criteria

- [ ] Both done
"""
    areas = plan_change_areas(body)
    assert areas == [
        "Add Resend lead emails on submit",
        "Remove phone contact option",
    ]


def test_child_issue_body_copies_acceptance_criteria() -> None:
    body = child_issue_body(55, "Remove phone", ISSUE_55_STYLE)
    assert "Child of #55" in body
    assert "## Scope" in body
    assert "Remove phone" in body
    assert "## Acceptance criteria" in body
    assert "Email only contact" in body
    assert extract_acceptance_section(ISSUE_55_STYLE).startswith("## Acceptance criteria")


def test_child_issue_body_fallback_when_no_acceptance() -> None:
    body = child_issue_body(9, "Wire webhook", "## Summary\n\nNo AC here.\n")
    assert "Implements scoped change from parent #9" in body
