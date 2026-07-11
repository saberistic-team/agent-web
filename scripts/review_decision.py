#!/usr/bin/env python3
"""Resolve reviewer decision from GitHub PR review events (fail closed).

Does not read local decision files. Requires at least one submitted PR review
on an open PR linked to the issue. Prints `approved`, `changes-requested`,
or `blocked` (terminal hard-fail — do not requeue Builder).
"""

from __future__ import annotations

import argparse
import sys

from github_api import api, split_repo


def linked_open_prs(repo: str, issue: int) -> list[dict]:
    owner, name = split_repo(repo)
    prs = api("GET", f"/repos/{owner}/{name}/pulls?state=open&per_page=100") or []
    needle = f"#{issue}"
    linked = [
        pr
        for pr in prs
        if needle in (pr.get("title") or "") or needle in (pr.get("body") or "")
    ]
    issue_data = api("GET", f"/repos/{owner}/{name}/issues/{issue}")
    if issue_data.get("pull_request"):
        pr_num = int(issue_data["number"])
        if not any(int(p["number"]) == pr_num for p in linked):
            linked.append(api("GET", f"/repos/{owner}/{name}/pulls/{pr_num}"))
    return linked


def latest_submitted_review(repo: str, pr_number: int) -> dict | None:
    owner, name = split_repo(repo)
    reviews = api("GET", f"/repos/{owner}/{name}/pulls/{pr_number}/reviews") or []
    submitted = [
        r
        for r in reviews
        if (r.get("state") or "").upper() in {"APPROVED", "CHANGES_REQUESTED"}
    ]
    if not submitted:
        return None
    return submitted[-1]


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
        if state == "APPROVED":
            print("approved")
            print(f"reviewer={user} pr={prs[0]['number']}", file=sys.stderr)
            return 0
        if state == "CHANGES_REQUESTED":
            body = (review.get("body") or "").lower()
            # Terminal hard-fails must not requeue Builder (infinite loop).
            if "terminal: true" in body or "worklog-only" in body:
                print("blocked")
                print(
                    f"reviewer={user} pr={prs[0]['number']} terminal=true",
                    file=sys.stderr,
                )
                return 0
            # Second+ changes-requested also blocks to stop ping-pong.
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
            if prior >= 2:
                print("blocked")
                print(
                    f"reviewer={user} pr={prs[0]['number']} prior_changes={prior}",
                    file=sys.stderr,
                )
                return 0
            print("changes-requested")
            print(f"reviewer={user} pr={prs[0]['number']}", file=sys.stderr)
            return 0
        print(f"FAIL: unexpected review state {state}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
