#!/usr/bin/env python3
"""GitHub Copilot cloud agent + code review adapters for the agent loop.

Builder: assign issue to Copilot coding agent (`copilot-swe-agent[bot]`), wait
for a linked PR, then hand off to Reviewer.

Reviewer: request Copilot code review (`copilot-pull-request-reviewer[bot]`),
collect comments, map to an ai_review-compatible verdict.

Requires a **user** token for assignment (PAT / OAuth). GitHub App installation
tokens are not supported for Copilot assignment — set secret `COPILOT_TOKEN`.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from github_api import GitHubError, api, post_issue_comment, split_repo, token

COPILOT_BUILDER = "copilot-swe-agent[bot]"
COPILOT_REVIEWER = "copilot-pull-request-reviewer[bot]"
DEFAULT_WAIT_SECONDS = int(os.environ.get("COPILOT_WAIT_SECONDS") or "900")
DEFAULT_POLL_SECONDS = int(os.environ.get("COPILOT_POLL_SECONDS") or "30")


def copilot_token() -> str:
    """User-to-server token required to assign Copilot (not App install tokens)."""
    value = (
        os.environ.get("COPILOT_TOKEN")
        or os.environ.get("COPILOT_ASSIGN_TOKEN")
        or ""
    ).strip()
    if value:
        return value
    # May work for review-request; assignment usually needs a user PAT.
    return token()


def copilot_enabled() -> bool:
    """True when we should attempt Copilot coding agent first."""
    force = (os.environ.get("CODEGEN_PROVIDER") or "").strip().lower()
    if force in {"models", "github-models", "openai", "chatgpt"}:
        return False
    if force == "copilot":
        return True
    if (os.environ.get("COPILOT_DISABLED") or "").lower() in {"1", "true", "yes"}:
        return False
    return bool(
        (os.environ.get("COPILOT_TOKEN") or os.environ.get("COPILOT_ASSIGN_TOKEN") or "").strip()
    )


def _api(
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    tok: str | None = None,
    timeout: int = 60,
) -> Any:
    url = path if path.startswith("http") else f"https://api.github.com{path}"
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {tok or copilot_token()}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "agent-web-copilot",
            **({"Content-Type": "application/json"} if body is not None else {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if not raw:
                return None
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise GitHubError(f"{method} {path} -> {exc.code}: {detail}") from exc


def build_custom_instructions(
    *,
    title: str,
    body: str,
    brief: Path,
    ui: bool = False,
) -> str:
    brief_text = brief.read_text(encoding="utf-8") if brief.is_file() else ""
    parts = [
        "You are the Builder for the agent-web repo.",
        "Implement ONLY this GitHub issue. Open a PR that references the issue.",
        "Commit messages must include the issue number like builder(#N): …",
        "PR body must include `Closes #N`.",
        "Prefer minimal diffs; add/update tests when behavior changes.",
        "Do not modify agent orchestration workflows unless the issue requires it.",
        "",
        f"Issue title: {title}",
        "",
        "## Issue body",
        body.strip() or "(empty)",
        "",
        "## Builder brief (excerpt)",
        brief_text[:4000],
    ]
    if ui:
        parts.extend(
            [
                "",
                "## UI/design constraints",
                "Brutal-minimalist, brand-first (navy #0c0f18 / #171d34, orange #d88730).",
                "Keep Archivo Black + IBM Plex Mono. Single wordmark in header.",
                "No purple gradients, cream+serif terracotta, newspaper layouts, or team roster.",
                "Reuse site/assets/site.css tokens; edit site/ HTML+CSS as needed.",
            ]
        )
    return "\n".join(parts)


def assign_copilot(
    repo: str,
    issue: int,
    *,
    base_branch: str | None = None,
    custom_instructions: str = "",
    model: str = "",
) -> dict[str, Any]:
    owner, name = split_repo(repo)
    if not base_branch:
        base_branch = api("GET", f"/repos/{owner}/{name}").get("default_branch") or "main"
    payload = {
        "assignees": [COPILOT_BUILDER],
        "agent_assignment": {
            "target_repo": repo,
            "base_branch": base_branch,
            "custom_instructions": custom_instructions[:8000],
            "custom_agent": "",
            "model": model or (os.environ.get("COPILOT_MODEL") or ""),
        },
    }
    return _api(
        "POST",
        f"/repos/{owner}/{name}/issues/{issue}/assignees",
        body=payload,
        tok=copilot_token(),
    )


def linked_prs(repo: str, issue: int) -> list[dict[str, Any]]:
    """Return only the strict resolver's single intentional candidate."""
    from github_api import unique_open_pr_or_none

    pr = unique_open_pr_or_none(repo, issue)
    return [pr] if pr else []


def wait_for_copilot_pr(
    repo: str,
    issue: int,
    *,
    timeout_seconds: int = DEFAULT_WAIT_SECONDS,
    poll_seconds: int = DEFAULT_POLL_SECONDS,
) -> dict[str, Any]:
    deadline = time.time() + timeout_seconds
    last: list[dict[str, Any]] = []
    while time.time() < deadline:
        last = linked_prs(repo, issue)
        # Prefer PRs authored by Copilot.
        copilot_prs = [
            p
            for p in last
            if "copilot" in ((p.get("user") or {}).get("login") or "").lower()
        ]
        if copilot_prs:
            return sorted(
                copilot_prs, key=lambda p: p.get("updated_at") or "", reverse=True
            )[0]
        if last:
            # Existing Builder PR updated after assign — accept newest.
            return sorted(last, key=lambda p: p.get("updated_at") or "", reverse=True)[0]
        time.sleep(poll_seconds)
    raise GitHubError(
        f"timed out waiting for Copilot PR on #{issue} "
        f"after {timeout_seconds}s (open linked PRs={len(last)})"
    )


def build_with_copilot(
    repo: str,
    issue: int,
    *,
    title: str,
    body: str,
    brief: Path,
    ui: bool = False,
) -> dict[str, Any]:
    """Assign Copilot coding agent and wait until a linked PR exists."""
    if not (
        os.environ.get("COPILOT_TOKEN") or os.environ.get("COPILOT_ASSIGN_TOKEN")
    ):
        raise GitHubError(
            "COPILOT_TOKEN secret required to assign Copilot coding agent "
            "(user PAT; App installation tokens are not supported)"
        )

    instructions = build_custom_instructions(
        title=title, body=body, brief=brief, ui=ui
    )
    post_issue_comment(
        repo,
        issue,
        (
            "### builder_copilot\n"
            f"- action: `assign`\n"
            f"- agent: `{COPILOT_BUILDER}`\n"
            "- status: waiting for PR\n"
        ),
    )
    assign_copilot(repo, issue, custom_instructions=instructions)
    pr = wait_for_copilot_pr(repo, issue)
    pr_number = int(pr["number"])
    comment = (
        "### builder_result\n"
        "- kind: `copilot`\n"
        f"- provider: `copilot`\n"
        f"- model: `{(os.environ.get('COPILOT_MODEL') or 'auto')}`\n"
        f"- pr: #{pr_number}\n"
        f"- pr_url: {pr.get('html_url')}\n"
        f"- head: `{(pr.get('head') or {}).get('ref')}`\n"
    )
    post_issue_comment(repo, issue, comment)
    return {
        "provider": "copilot",
        "model": os.environ.get("COPILOT_MODEL") or "auto",
        "pr_number": pr_number,
        "pr_url": pr.get("html_url"),
        "branch": (pr.get("head") or {}).get("ref"),
        "commit_message": f"builder(#{issue}): copilot coding agent",
        "files": [],
    }


def request_copilot_review(repo: str, pr_number: int) -> dict[str, Any]:
    owner, name = split_repo(repo)
    return _api(
        "POST",
        f"/repos/{owner}/{name}/pulls/{pr_number}/requested_reviewers",
        body={"reviewers": [COPILOT_REVIEWER]},
        tok=copilot_token(),
    )


def wait_for_copilot_review(
    repo: str,
    pr_number: int,
    *,
    timeout_seconds: int = 240,
    poll_seconds: int = 20,
) -> dict[str, Any]:
    """Poll until Copilot submits a review or comments; return aggregated signal."""
    owner, name = split_repo(repo)
    deadline = time.time() + timeout_seconds
    # Re-request once mid-wait if nothing arrives.
    re_requested = False
    while time.time() < deadline:
        reviews = (
            api("GET", f"/repos/{owner}/{name}/pulls/{pr_number}/reviews") or []
        )
        comments = (
            api("GET", f"/repos/{owner}/{name}/pulls/{pr_number}/comments") or []
        )
        copilot_reviews = [
            r
            for r in reviews
            if "copilot" in ((r.get("user") or {}).get("login") or "").lower()
        ]
        copilot_comments = [
            c
            for c in comments
            if "copilot" in ((c.get("user") or {}).get("login") or "").lower()
        ]
        if copilot_reviews or copilot_comments:
            return {
                "reviews": copilot_reviews,
                "comments": copilot_comments,
            }
        if not re_requested and time.time() > deadline - (timeout_seconds / 2):
            try:
                request_copilot_review(repo, pr_number)
            except Exception:
                pass
            re_requested = True
        time.sleep(poll_seconds)
    return {"reviews": [], "comments": []}


def copilot_review_verdict(repo: str, issue: int, pr_number: int) -> dict[str, Any]:
    """Request Copilot code review and map to ai_review-compatible dict."""
    try:
        request_copilot_review(repo, pr_number)
    except Exception as exc:
        # Already requested / permissions — continue to poll existing reviews.
        note = f"request_note: {exc}"
    else:
        note = "requested"

    data = wait_for_copilot_review(repo, pr_number)
    reviews = data.get("reviews") or []
    comments = data.get("comments") or []
    reasons: list[str] = []
    for c in comments[:12]:
        body = (c.get("body") or "").strip()
        path = c.get("path") or ""
        if body:
            reasons.append(f"{path}: {body[:240]}" if path else body[:280])
    states = [(r.get("state") or "").upper() for r in reviews]
    if any(s == "CHANGES_REQUESTED" for s in states):
        decision = "changes-requested"
        meets = False
    elif any(s == "APPROVED" for s in states) and not reasons:
        decision = "approved"
        meets = True
    elif reasons:
        # Inline comments without formal CHANGES_REQUESTED — treat as soft fail
        # only when clearly blocking language appears.
        blocking = any(
            re_search_block(r) for r in reasons
        )
        decision = "changes-requested" if blocking else "approved"
        meets = not blocking
        if not blocking:
            reasons = ["Copilot left nits only; non-blocking"] + reasons[:6]
    elif reviews:
        decision = "approved"
        meets = True
        reasons = ["Copilot review present without changes requested"]
    else:
        decision = "changes-requested"
        meets = False
        reasons = [
            "Copilot code review did not arrive in time; "
            "fail closed (set COPILOT_TOKEN / enable code review, or rely on OpenAI/Models AI)"
        ]

    return {
        "decision": decision,
        "meets_acceptance": meets,
        "reasons": reasons[:12],
        "summary": f"Copilot code review ({note}); reviews={len(reviews)} comments={len(comments)}",
        "model": "copilot-code-review",
        "scaffold_sync": False,
        "provider": "copilot",
    }


def re_search_block(text: str) -> bool:
    import re

    return bool(
        re.search(
            r"\b(must|critical|security|vulnerability|broken|incorrect|fail|"
            r"missing test|do not merge|blocker)\b",
            text,
            re.I,
        )
    )
