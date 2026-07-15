#!/usr/bin/env python3
"""Resolve reviewer decision from GitHub PR review events (fail closed).

Does not read local decision files. Requires at least one submitted PR review
on an open PR linked to the issue. Prints `approved`, `changes-requested`,
or `blocked` (terminal hard-fail — do not requeue Builder).
"""

from __future__ import annotations

import argparse
import re
import sys
import time

from github_api import api, split_repo

# Builder can (and must) fix these — never escalate to status:blocked solely
# because Reviewer has requested changes more than once.
_FIXABLE_RE = re.compile(
    r"coverage|missing tests|without test file|check `|screenshots failed|"
    r"acceptance criteria incomplete|visual readability|out of frame|"
    r"overflow|clipped|pytest|test_|"
    r"admin preview empty data|empty shell|mock rows|ADMIN_PREVIEW|"
    r"admin desktop nav invisible|admin-nav-desktop|admin-nav-link|"
    r"merge conflict|mergeable|mergeability|return to Builder|"
    r"return to Docs|agent-updates stub|docs PR|type:docs PR",
    re.I,
)
_TERMINAL_RE = re.compile(r"terminal:\s*true|worklog-only", re.I)


def is_fixable_changes_requested(body: str) -> bool:
    """True when hard-fails are Builder work (coverage, tests, visual, CI, conflicts)."""
    text = body or ""
    if _TERMINAL_RE.search(text):
        return False
    return bool(_FIXABLE_RE.search(text))


def resolve_decision(
    *,
    latest_state: str,
    latest_body: str,
    prior_changes_requested: int,
) -> str:
    """Map the latest submitted review into an orchestration decision."""
    state = (latest_state or "").upper()
    if state == "APPROVED":
        return "approved"
    if state != "CHANGES_REQUESTED":
        raise ValueError(f"unexpected review state {latest_state!r}")
    body = latest_body or ""
    if _TERMINAL_RE.search(body):
        return "blocked"
    # Coverage / tests / visual overflow / CI / merge conflicts always requeue Builder.
    if is_fixable_changes_requested(body):
        return "changes-requested"
    # Non-fixable judgment ping-pong: stop after the second request.
    if prior_changes_requested >= 2:
        return "blocked"
    return "changes-requested"


def linked_open_prs(repo: str, issue: int) -> list[dict]:
    from github_api import linked_open_prs as _linked_open_prs

    linked = list(_linked_open_prs(repo, issue))
    owner, name = split_repo(repo)
    issue_data = api("GET", f"/repos/{owner}/{name}/issues/{issue}")
    if issue_data.get("pull_request"):
        pr_num = int(issue_data["number"])
        if not any(int(p["number"]) == pr_num for p in linked):
            linked.append(api("GET", f"/repos/{owner}/{name}/pulls/{pr_num}"))
    return linked


def latest_submitted_review(
    repo: str,
    pr_number: int,
    *,
    attempts: int = 8,
    delay_sec: float = 2.0,
) -> dict | None:
    """Return the latest APPROVED/CHANGES_REQUESTED review, retrying briefly.

    Reviewer Actions posts the PR review then immediately runs this script.
    GitHub's reviews list can lag a second or two — failing closed with
    ``no submitted … review`` left issues stuck on ``agent:reviewer`` while
    the PR already had a decision (#182 / #188).
    """
    owner, name = split_repo(repo)
    for attempt in range(max(1, attempts)):
        reviews = api("GET", f"/repos/{owner}/{name}/pulls/{pr_number}/reviews") or []
        submitted = [
            r
            for r in reviews
            if (r.get("state") or "").upper() in {"APPROVED", "CHANGES_REQUESTED"}
        ]
        if submitted:
            return submitted[-1]
        if attempt + 1 < attempts:
            time.sleep(delay_sec)
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--issue", required=True, type=int)
    args = parser.parse_args(argv)

    try:
        prs = linked_open_prs(args.repo, args.issue)
        if not prs:
            print("FAIL: no open PR linked to issue", file=sys.stderr)
            return 1
        prs.sort(key=lambda p: p.get("updated_at") or "", reverse=True)
        review = latest_submitted_review(args.repo, int(prs[0]["number"]))
        if review is None:
            print(
                "FAIL: no submitted APPROVED/CHANGES_REQUESTED review on linked PR",
                file=sys.stderr,
            )
            return 1
        state = (review.get("state") or "").upper()
        user = (review.get("user") or {}).get("login", "unknown")
        owner, name = split_repo(args.repo)
        reviews = (
            api("GET", f"/repos/{owner}/{name}/pulls/{int(prs[0]['number'])}/reviews")
            or []
        )
        prior = sum(
            1
            for r in reviews
            if (r.get("state") or "").upper() == "CHANGES_REQUESTED"
        )
        try:
            decision = resolve_decision(
                latest_state=state,
                latest_body=review.get("body") or "",
                prior_changes_requested=prior,
            )
        except ValueError as exc:
            print(f"FAIL: {exc}", file=sys.stderr)
            return 1
        print(decision)
        print(
            f"reviewer={user} pr={prs[0]['number']} prior_changes={prior}",
            file=sys.stderr,
        )
        return 0
    except Exception as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
