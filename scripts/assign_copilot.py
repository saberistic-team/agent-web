#!/usr/bin/env python3
"""Assign GitHub Copilot cloud agent to an issue (user-token required)."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from github_api import GitHubError, api, post_issue_comment, split_repo, token


COPILOT_LOGIN = "copilot-swe-agent[bot]"


def assign_token() -> str:
    """Copilot assignment requires a user-to-server token (not App install)."""
    value = (
        os.environ.get("COPILOT_ASSIGN_TOKEN")
        or os.environ.get("GH_TOKEN")
        or os.environ.get("GITHUB_TOKEN")
    )
    if not value:
        raise GitHubError(
            "missing COPILOT_ASSIGN_TOKEN (fine-grained PAT / user token required "
            "to assign Copilot cloud agent)"
        )
    return value


def default_branch(repo: str) -> str:
    owner, name = split_repo(repo)
    data = api("GET", f"/repos/{owner}/{name}")
    return data.get("default_branch") or "main"


def build_instructions(brief: Path, title: str, body: str) -> str:
    brief_text = brief.read_text(encoding="utf-8") if brief.is_file() else ""
    # Keep prompt bounded for the assignment API.
    brief_snip = brief_text.strip()[:4000]
    body_snip = (body or "").strip()[:6000]
    return (
        "You are implementing this GitHub issue for the Builder role.\n"
        "Follow AGENTS/builder.md constraints.\n"
        "- Work on a non-default branch; open one focused pull request.\n"
        "- Never push directly to main/master.\n"
        "- Add or update tests when behavior changes.\n"
        "- Stay within issue scope; no drive-by refactors.\n"
        "- Do not invent credentials or revive removed product surfaces.\n\n"
        f"## Builder brief\n{brief_snip}\n\n"
        f"## Issue title\n{title}\n\n"
        f"## Issue body\n{body_snip}\n"
    )


def assign_copilot(
    repo: str,
    issue: int,
    *,
    instructions: str,
    model: str | None = None,
    base_branch: str | None = None,
) -> dict:
    owner, name = split_repo(repo)
    branch = base_branch or default_branch(repo)
    payload = {
        "assignees": [COPILOT_LOGIN],
        "agent_assignment": {
            "target_repo": repo,
            "base_branch": branch,
            "custom_instructions": instructions,
            "custom_agent": "",
            "model": model or "",
        },
    }
    return api(
        "POST",
        f"/repos/{owner}/{name}/issues/{issue}/assignees",
        body=payload,
        token_override=assign_token(),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--issue", required=True, type=int)
    parser.add_argument("--brief", type=Path, default=Path("AGENTS/builder.md"))
    parser.add_argument(
        "--model",
        default=os.environ.get("COPILOT_MODEL", ""),
        help="Optional Copilot model id; empty = auto",
    )
    parser.add_argument("--title", default="")
    parser.add_argument("--body", default="")
    args = parser.parse_args(argv)

    try:
        # Prefer live issue body when not passed.
        title = args.title
        body = args.body
        if not title or not body:
            owner, name = split_repo(args.repo)
            data = api("GET", f"/repos/{owner}/{name}/issues/{args.issue}")
            title = title or data.get("title") or f"issue-{args.issue}"
            body = body or data.get("body") or ""

        instructions = build_instructions(args.brief, title, body)
        result = assign_copilot(
            args.repo,
            args.issue,
            instructions=instructions,
            model=args.model or None,
        )
        assignees = [
            a.get("login")
            for a in (result.get("assignees") or [])
            if isinstance(a, dict)
        ]
        post_issue_comment(
            args.repo,
            args.issue,
            (
                "### builder_copilot\n"
                f"- assignee: `{COPILOT_LOGIN}`\n"
                f"- model: `{args.model or 'auto'}`\n"
                f"- assignees_now: {', '.join(f'`{a}`' for a in assignees) or '_none_'}\n"
                "- next: wait for Copilot PR, then Reviewer\n"
            ),
        )
        print(f"assigned {COPILOT_LOGIN} to #{args.issue}")
        return 0
    except Exception as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    # Ensure scripts/ is importable when run from repo root.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main())
