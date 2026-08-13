#!/usr/bin/env python3
"""Mirror selected issue labels (and milestone) onto linked pull requests.

Issue labels remain the orchestration source of truth. PRs only carry
``type:*``, ``priority:*``, and ``review:*`` for human filtering — never
``agent:*`` or ``status:*``. When the linked issue has a milestone, copy it
onto the PR as well. See docs/LABELS.md.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, Iterable

from github_api import add_labels, api, delete_label, split_repo, unique_open_pr_or_none
from milestones import assign_milestone, issue_milestone_number

# Axes that may appear on PRs (mirrors of the linked issue).
PR_MIRROR_PREFIXES = ("type:", "priority:", "review:")
REVIEW_LABELS = frozenset(
    {
        "review:needs-review",
        "review:approved",
        "review:changes-requested",
    }
)


def get_issue(repo: str, number: int) -> dict[str, Any]:
    owner, name = split_repo(repo)
    return api("GET", f"/repos/{owner}/{name}/issues/{number}") or {}


def get_labels(repo: str, number: int) -> set[str]:
    data = get_issue(repo, number)
    return {label["name"] for label in data.get("labels") or []}


def mirror_pr_milestone(repo: str, issue: int, pr: int) -> int | None:
    """Copy the issue milestone onto ``pr`` when the issue has one.

    Uses the Issues API (works for PR numbers). Returns the milestone number
    applied, or None when the issue has no milestone (critical/optional).
    """
    number = issue_milestone_number(get_issue(repo, issue))
    if number is None:
        return None
    assign_milestone(repo, pr, number)
    return number


def _axis_label(labels: Iterable[str], prefix: str) -> str | None:
    return next((label for label in labels if label.startswith(prefix)), None)


def desired_pr_labels(
    issue_labels: set[str] | list[str],
    *,
    review: str | None = None,
    default_review: str | None = None,
) -> list[str]:
    """Compute the PR mirror set from issue labels + optional review override."""
    labels = set(issue_labels)
    out: list[str] = []
    for prefix in ("type:", "priority:"):
        found = _axis_label(labels, prefix)
        if found:
            out.append(found)

    review_label = review
    if review_label is None:
        review_label = _axis_label(labels, "review:") or default_review
    if review_label:
        if review_label not in REVIEW_LABELS:
            raise ValueError(f"invalid review label: {review_label!r}")
        out.append(review_label)
    return out


def clear_pr_mirror_labels(repo: str, pr: int) -> None:
    for label in list(get_labels(repo, pr)):
        if label.startswith(PR_MIRROR_PREFIXES):
            delete_label(repo, pr, label)


def apply_pr_mirror(
    repo: str,
    issue: int,
    pr: int,
    *,
    review: str | None = None,
    default_review: str | None = None,
) -> list[str]:
    """Replace type/priority/review on ``pr`` from the linked ``issue``.

    Also copies the issue milestone onto the PR when set. Returns the labels
    applied.
    """
    issue_data = get_issue(repo, issue)
    issue_labels = {label["name"] for label in issue_data.get("labels") or []}
    desired = desired_pr_labels(
        issue_labels,
        review=review,
        default_review=default_review,
    )
    clear_pr_mirror_labels(repo, pr)
    if desired:
        add_labels(repo, pr, desired)
    milestone_number = issue_milestone_number(issue_data)
    if milestone_number is not None:
        assign_milestone(repo, pr, milestone_number)
    return desired


def apply_to_linked_prs(
    repo: str,
    issue: int,
    *,
    pr: int | None = None,
    review: str | None = None,
    default_review: str | None = None,
) -> dict[int, list[str]]:
    """Apply mirror labels to one PR or all open PRs linked to ``issue``."""
    if pr is not None:
        targets = [pr]
    else:
        resolved = unique_open_pr_or_none(repo, issue)
        targets = [int(resolved["number"])] if resolved else []
    applied: dict[int, list[str]] = {}
    for number in targets:
        applied[number] = apply_pr_mirror(
            repo,
            issue,
            number,
            review=review,
            default_review=default_review,
        )
    return applied


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--issue", required=True, type=int)
    parser.add_argument(
        "--pr",
        type=int,
        default=None,
        help="PR number (default: all open PRs linked to the issue)",
    )
    parser.add_argument(
        "--review",
        default=None,
        choices=sorted(REVIEW_LABELS),
        help="Force the review:* label on the PR(s)",
    )
    parser.add_argument(
        "--default-review",
        default=None,
        choices=sorted(REVIEW_LABELS),
        help="Use when neither --review nor an issue review:* label is set",
    )
    args = parser.parse_args(argv)

    result = apply_to_linked_prs(
        args.repo,
        args.issue,
        pr=args.pr,
        review=args.review,
        default_review=args.default_review,
    )
    if not result:
        print(f"no open PR linked to #{args.issue}; nothing labeled", file=sys.stderr)
        return 0
    for number, labels in sorted(result.items()):
        print(f"pr #{number}: {', '.join(labels) if labels else '(cleared mirror labels)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
