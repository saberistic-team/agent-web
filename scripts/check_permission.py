#!/usr/bin/env python3
"""Fail-closed GitHub collaborator permission check for the agent gate.

Always records a line in trace/agent-trace.jsonl and posts an issue comment
so the result is visible in the GitHub UI/API (never silent).
"""

from __future__ import annotations

import argparse
import sys
import urllib.parse

from github_api import GitHubError, api, post_issue_comment, token
from write_trace import write_event

LEVEL_RANK = {
    "none": 0,
    "read": 1,
    "triage": 2,
    "write": 3,
    "maintain": 4,
    "admin": 5,
}


def github_permission(repo: str, actor: str) -> str:
    """Return effective permission for a human collaborator or known App bot."""
    owner, name = repo.split("/", 1)

    # GitHub App bots are not repo collaborators — map installation scopes.
    if actor.endswith("[bot]"):
        return bot_installation_permission(owner, name, actor)

    path = (
        f"/repos/{owner}/{name}/collaborators/"
        f"{urllib.parse.quote(actor)}/permission"
    )
    payload = api("GET", path)
    level = (payload.get("permission") or "").lower()
    if level not in LEVEL_RANK:
        raise GitHubError(f"unknown permission value from API: {level!r}")
    return level


def bot_installation_permission(owner: str, repo_name: str, actor: str) -> str:
    """Derive a collaborator-equivalent level from the App installation scopes."""
    slug_hint = actor[: -len("[bot]")].lower().replace("_", "-")
    installations = api("GET", f"/orgs/{owner}/installations?per_page=100")
    nodes = installations.get("installations") or []
    match = None
    for inst in nodes:
        slug = (inst.get("app_slug") or "").lower()
        if slug == slug_hint or slug_hint.endswith(slug) or slug in slug_hint:
            match = inst
            break
    if match is None:
        raise GitHubError(f"no org installation found for bot actor {actor!r}")

    perms = match.get("permissions") or {}
    # Map App repo permissions → collaborator-equivalent rank for --min-level.
    if perms.get("administration") == "write":
        return "admin"
    if perms.get("contents") == "write":
        return "write"
    if perms.get("pull_requests") == "write" or perms.get("issues") == "write":
        # Reviewer/Planner bots authorize gates via their App scopes, not
        # collaborator membership (bots always show as permission none there).
        return "write"
    if perms.get("contents") == "read":
        return "read"
    if perms.get("metadata") == "read":
        return "read"
    return "none"


def meets_minimum(actual: str, required: str) -> bool:
    return LEVEL_RANK[actual] >= LEVEL_RANK[required]


def comment_body(record: dict) -> str:
    lines = [
        "### permission_check",
        f"- result: `{record['outcome']}`",
        f"- actor: `{record['actor']}`",
        f"- repo: `{record['repo']}`",
        f"- required: `{record['required']}`",
        f"- actual: `{record.get('actual')}`",
        f"- ts: `{record['ts']}`",
    ]
    if record.get("detail"):
        lines.append(f"- detail: {record['detail']}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--actor", required=True, help="GitHub login to check")
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--issue", required=True, type=int, help="Issue number")
    parser.add_argument(
        "--min-level",
        required=True,
        choices=sorted(LEVEL_RANK, key=LEVEL_RANK.get),
        help="Minimum required permission level",
    )
    args = parser.parse_args(argv)

    meta = {
        "actor": args.actor,
        "repo": args.repo,
        "required": args.min_level,
        "actual": None,
        "detail": None,
        "outcome": "fail",
        "ts": None,
    }

    def persist_and_comment() -> None:
        try:
            written = write_event(
                role="gate",
                action="permission_check",
                outcome=meta["outcome"],
                issue=args.issue,
                model=None,
                cost_usd=0.0,
            )
            meta["ts"] = written["ts"]
        except OSError as exc:
            print(f"trace write failed: {exc}", file=sys.stderr)
        try:
            token()
            post_issue_comment(args.repo, args.issue, comment_body(meta))
        except Exception as exc:
            print(f"FAIL: could not post permission_check comment: {exc}", file=sys.stderr)
            raise

    try:
        actual = github_permission(args.repo, args.actor)
        meta["actual"] = actual
        if meets_minimum(actual, args.min_level):
            meta["outcome"] = "pass"
            persist_and_comment()
            print(
                f"PASS: {args.actor} has {actual} on {args.repo} "
                f"(required {args.min_level})"
            )
            return 0
        meta["detail"] = "insufficient permission"
        persist_and_comment()
        print(
            f"FAIL: {args.actor} has {actual} on {args.repo} "
            f"(required {args.min_level})",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:
        meta["detail"] = str(exc)
        try:
            persist_and_comment()
        except Exception:
            try:
                write_event(
                    role="gate",
                    action="permission_check",
                    outcome="fail",
                    issue=args.issue,
                    cost_usd=0.0,
                )
            except OSError as trace_exc:
                print(f"trace write failed: {trace_exc}", file=sys.stderr)
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
