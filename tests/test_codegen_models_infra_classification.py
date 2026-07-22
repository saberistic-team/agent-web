"""Regression test: `is_agent_infra_issue` must not misclassify ordinary
product/bug-fix issues as Reviewer/screenshot *infra* work just because they
require a Playwright test in their acceptance criteria.

Issue #237 ("Keep admin identity, Public Site and Sign Out actions visible at
every width") required a Playwright layout assertion. The old regex matched
any bare `playwright` mention, so Builder took the no-op docs-sync shortcut
(`kind: infra-screenshots`) instead of running real codegen, on every single
dispatch. Reviewer correctly rejected the no-op PR every time, producing an
unbounded Builder<->Reviewer ping-pong loop (~1000 issue comments / ~75 empty
commits over several hours) that never converged.
"""

from __future__ import annotations

import pytest

from codegen_models import is_agent_infra_issue

ISSUE_237_TITLE = "Keep admin identity, Public Site and Sign Out actions visible at every width"
ISSUE_237_BODY = """## Summary

Repair admin top-bar overflow so long usernames or constrained viewports cannot clip or hide Public Site and Sign Out.

## Required tests

- Add a Playwright layout assertion that Public Site and Sign Out bounding boxes remain inside the viewport.
- Test a long unbroken username and a normal email address.

## Acceptance criteria

- [ ] Desktop, tablet, and mobile screenshots include a long-username fixture
"""

ISSUE_35_TITLE = "Reviewer: headless screenshots of deployed app on PR before approve"
ISSUE_35_BODY = """- [ ] On reviewer path for a linked open PR, start headless browser against
  deploy URL.
- [ ] Capture at least desktop viewport screenshots of `/` and `/about`.
- [ ] Post screenshots to the PR before submitting APPROVE.
"""

ISSUE_166_TITLE = "Capture expected authenticated admin error pages in reviewer screenshots"
ISSUE_166_BODY = """- [ ] Screenshot targets can explicitly declare an expected HTTP status code
  such as 404 or 503.
- [ ] The capture probe accepts the page only when its actual status matches
  the declared expected status.
"""

ISSUE_234_TITLE = "Render production admin page components in preview screenshot mode"
ISSUE_234_BODY = """## Summary

Make pre-deployment screenshots exercise the same page renderers used by
authenticated production admin pages.

## Required tests

- Keep desktop and mobile Playwright captures stable.
"""


@pytest.mark.unit
@pytest.mark.parametrize(
    ("title", "body"),
    [
        (ISSUE_237_TITLE, ISSUE_237_BODY),
        (
            "Eliminate cross-source username enumeration in admin login throttling",
            "Add a Playwright regression test that exercises the login throttling "
            "endpoint and asserts identical screenshots for existing and "
            "nonexistent candidates.",
        ),
        (
            "Fix contact archive button styling",
            "## Required tests\n\n- Add a Playwright test asserting the button "
            "screenshot matches the themed style.",
        ),
    ],
)
def test_playwright_mention_alone_is_not_infra_work(title: str, body: str) -> None:
    """A bare `playwright` (or lone `screenshot`) mention in a product issue's
    test requirements must not trigger the no-op infra docs-sync shortcut."""
    assert is_agent_infra_issue(title, body) is False


@pytest.mark.unit
@pytest.mark.parametrize(
    ("title", "body"),
    [
        (ISSUE_35_TITLE, ISSUE_35_BODY),
        (ISSUE_166_TITLE, ISSUE_166_BODY),
        (ISSUE_234_TITLE, ISSUE_234_BODY),
    ],
)
def test_genuine_reviewer_screenshot_infra_still_detected(title: str, body: str) -> None:
    """Real Reviewer/screenshot-pipeline infra issues must still take the
    docs-sync shortcut (no regression from tightening the heuristic)."""
    assert is_agent_infra_issue(title, body) is True
