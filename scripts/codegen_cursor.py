#!/usr/bin/env python3
"""Builder codegen via Cursor Agent SDK (cloud runtime).

Uses a Cursor-hosted cloud agent against this GitHub repo with auto_create_pr.
Requires CURSOR_API_KEY (user or team service-account key).
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from github_api import GitHubError, api, post_issue_comment, split_repo

DEFAULT_CURSOR_MODEL = "composer-2.5"


def cursor_api_key() -> str | None:
    value = os.environ.get("CURSOR_API_KEY")
    return value.strip() if value and value.strip() else None


def cursor_model() -> str:
    return (os.environ.get("CURSOR_MODEL") or DEFAULT_CURSOR_MODEL).strip()


def _repo_https_url(repo: str) -> str:
    owner, name = split_repo(repo)
    return f"https://github.com/{owner}/{name}"


def _parent_context(repo: str, body: str) -> str:
    m = re.search(r"(?:Parent|Child of)\s*:?\s*#(\d+)", body, re.I)
    if not m:
        return ""
    owner, name = split_repo(repo)
    try:
        pdata = api("GET", f"/repos/{owner}/{name}/issues/{m.group(1)}")
    except Exception:
        return ""
    return (
        f"\n## Parent issue #{m.group(1)}: {pdata.get('title')}\n"
        f"{(pdata.get('body') or '')[:12000]}\n"
    )


def build_prompt(
    *,
    repo: str,
    issue: int,
    title: str,
    body: str,
    brief: Path,
) -> str:
    brief_text = brief.read_text(encoding="utf-8") if brief.is_file() else ""
    design = Path("docs/DESIGN.md")
    design_text = design.read_text(encoding="utf-8")[:4000] if design.is_file() else ""
    brand = Path(".github/copilot-instructions.md")
    brand_text = brand.read_text(encoding="utf-8")[:4000] if brand.is_file() else ""
    parent = _parent_context(repo, body)
    return (
        f"You are the Builder agent for `{repo}`.\n"
        f"Implement GitHub issue #{issue} end-to-end on a new branch and open a PR.\n\n"
        "Hard rules:\n"
        f"- PR title like `builder: {title} (#{issue})`\n"
        f"- PR body MUST include `Closes #{issue}`\n"
        f"- Commit messages MUST include `builder(#{issue}): …`\n"
        "- Never push to main/master; only work on a feature branch\n"
        "- Stay in scope of this issue; no drive-by refactors\n"
        "- Add/update tests under tests/ when behavior changes\n"
        "- Follow brutal-minimalist brand rules below for any UI work\n"
        "- Live site reference: https://saberistic.com/\n\n"
        f"## Issue #{issue}: {title}\n"
        f"{body.strip() or '(empty)'}\n"
        f"{parent}\n"
        f"## Builder brief\n{brief_text[:5000]}\n\n"
        f"## Brand / agent instructions\n{brand_text}\n\n"
        f"## Design notes\n{design_text}\n"
    )


def _pr_number_from_url(url: str) -> int | None:
    if not url:
        return None
    path = urlparse(url).path.rstrip("/")
    m = re.search(r"/pull/(\d+)$", path)
    return int(m.group(1)) if m else None


def _wait_for_linked_pr(repo: str, issue: int, *, timeout_s: int = 180) -> dict | None:
    from codegen_models import linked_open_prs

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        prs = linked_open_prs(repo, issue)
        if prs:
            return prs[0]
        time.sleep(5)
    return None


def build_with_cursor(
    repo: str,
    issue: int,
    *,
    title: str,
    body: str,
    brief: Path,
) -> dict[str, Any]:
    key = cursor_api_key()
    if not key:
        raise GitHubError("missing CURSOR_API_KEY for Cursor SDK codegen")

    try:
        from cursor_sdk import (
            Agent,
            CloudAgentOptions,
            CloudRepository,
            CursorAgentError,
        )
    except ImportError as exc:
        raise GitHubError(
            "cursor-sdk is not installed; pip install -r requirements-agents.txt"
        ) from exc

    model = cursor_model()
    prompt = build_prompt(
        repo=repo, issue=issue, title=title, body=body, brief=brief
    )
    repo_url = _repo_https_url(repo)
    owner, name = split_repo(repo)
    default = api("GET", f"/repos/{owner}/{name}").get("default_branch") or "main"

    try:
        with Agent.create(
            model=model,
            api_key=key,
            name=f"builder-{issue}",
            cloud=CloudAgentOptions(
                repos=[
                    CloudRepository(
                        url=repo_url,
                        starting_ref=default,
                    )
                ],
                auto_create_pr=True,
                skip_reviewer_request=True,
            ),
        ) as agent:
            agent_id = getattr(agent, "agent_id", None) or getattr(
                agent, "agentId", None
            )
            run = agent.send(prompt)
            run_id = getattr(run, "id", None)
            result = run.wait()
    except CursorAgentError as exc:
        retryable = getattr(exc, "is_retryable", False)
        raise GitHubError(
            f"Cursor SDK startup failed (retryable={retryable}): {exc}"
        ) from exc

    status = getattr(result, "status", None)
    if status != "finished":
        raise GitHubError(
            f"Cursor cloud run did not finish: status={status!r} "
            f"agent_id={getattr(result, 'agent_id', '')} "
            f"run_id={getattr(result, 'id', '')} "
            f"result={getattr(result, 'result', '')[:500]!r}"
        )

    branch = ""
    pr_url = ""
    git = getattr(result, "git", None)
    branches = getattr(git, "branches", None) or ()
    if branches:
        branch = getattr(branches[0], "branch", "") or ""
        pr_url = getattr(branches[0], "pr_url", "") or ""

    pr_number = _pr_number_from_url(pr_url)
    created = bool(pr_number)
    if not pr_number:
        linked = _wait_for_linked_pr(repo, issue)
        if linked:
            pr_number = int(linked["number"])
            pr_url = linked.get("html_url") or pr_url
            branch = linked.get("head", {}).get("ref") or branch
            created = False
        else:
            raise GitHubError(
                "Cursor run finished but no PR was found. "
                f"agent_id={getattr(result, 'agent_id', '')} "
                f"run_id={getattr(result, 'id', '')} "
                f"git={git!r}"
            )

    # Ensure issue linkage for Gate / Reviewer if Cursor omitted Closes.
    try:
        pr = api("GET", f"/repos/{owner}/{name}/pulls/{pr_number}")
        pr_body = pr.get("body") or ""
        if f"#{issue}" not in pr_body:
            api(
                "PATCH",
                f"/repos/{owner}/{name}/pulls/{pr_number}",
                body={
                    "body": (
                        f"Closes #{issue}\n\n{pr_body}".strip()
                        + "\n\n### Codegen\n"
                        f"- provider: `cursor`\n"
                        f"- model: `{model}`\n"
                        f"- agent_id: `{getattr(result, 'agent_id', '')}`\n"
                        f"- run_id: `{getattr(result, 'id', '')}`\n"
                    )
                },
            )
    except Exception:
        pass

    comment = (
        "### builder_result\n"
        "- kind: `cursor`\n"
        f"- model: `{model}`\n"
        f"- agent_id: `{getattr(result, 'agent_id', agent_id or '')}`\n"
        f"- run_id: `{getattr(result, 'id', run_id or '')}`\n"
        f"- branch: `{branch or 'unknown'}`\n"
        f"- pr: #{pr_number}\n"
        f"- pr_url: {pr_url or '(none)'}\n"
        f"- created_pr: `{str(created).lower()}`\n"
        f"- summary: {(getattr(result, 'result', '') or '')[:500]}\n"
    )
    post_issue_comment(repo, issue, comment)
    return {
        "provider": "cursor",
        "model": model,
        "ui_design": False,
        "branch": branch,
        "pr": pr_number,
        "files": [],
        "created_pr": created,
        "agent_id": getattr(result, "agent_id", ""),
        "run_id": getattr(result, "id", ""),
    }
