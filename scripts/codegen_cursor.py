#!/usr/bin/env python3
"""Builder codegen via Cursor Agent SDK.

Default runtime in Actions is **local** (edits the checked-out workspace; Builder
App commits + opens the PR). Cloud (`CURSOR_RUNTIME=cloud`) needs the Cursor
account's GitHub integration to see this repo.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from github_api import GitHubError, api, post_issue_comment, split_repo
from pr_labels import apply_pr_mirror

DEFAULT_CURSOR_MODEL = "composer-2.5"
SKIP_PATH_PREFIXES = (
    ".git/",
    ".venv/",
    "trace/",
    ".agent/",
    "__pycache__/",
    "node_modules/",
)


def cursor_api_key() -> str | None:
    value = os.environ.get("CURSOR_API_KEY")
    return value.strip() if value and value.strip() else None


def cursor_model() -> str:
    return (os.environ.get("CURSOR_MODEL") or DEFAULT_CURSOR_MODEL).strip()


def cursor_runtime() -> str:
    """local (default in CI) | cloud."""
    forced = (os.environ.get("CURSOR_RUNTIME") or "").strip().lower()
    if forced in {"local", "cloud"}:
        return forced
    if os.environ.get("GITHUB_ACTIONS", "").lower() in {"1", "true"}:
        return "local"
    return "local"


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
    runtime: str,
) -> str:
    brief_text = brief.read_text(encoding="utf-8") if brief.is_file() else ""
    design = Path("docs/DESIGN.md")
    design_text = design.read_text(encoding="utf-8")[:4000] if design.is_file() else ""
    brand = Path(".github/copilot-instructions.md")
    brand_text = brand.read_text(encoding="utf-8")[:4000] if brand.is_file() else ""
    parent = _parent_context(repo, body)
    if runtime == "local":
        ship_rules = (
            "Hard rules (local Actions checkout):\n"
            "- Edit files in this workspace only; do NOT git commit, push, or open a PR\n"
            "- The Builder workflow will commit your file changes and open the PR\n"
            f"- Stay in scope of issue #{issue}; no drive-by refactors\n"
            "- Add/update tests under tests/ when behavior changes\n"
            "- Follow brutal-minimalist brand rules below for any UI work\n"
            "- Live site reference: https://saberistic.com/\n"
            "- Do not modify .github/workflows agent orchestration unless required\n"
            "- Binary assets (PNG/JPEG/WebP) must remain valid binary files\n"
            "- New admin/UI pages MUST include ADMIN_PREVIEW_MODE randomized "
            "mock data (see app/admin_preview.py + AGENTS/builder.md) so "
            "Reviewer screenshots are not empty shells\n"
        )
    else:
        ship_rules = (
            "Hard rules (cloud):\n"
            f"- PR title like `builder: {title} (#{issue})`\n"
            f"- PR body MUST include `Closes #{issue}`\n"
            f"- Commit messages MUST include `builder(#{issue}): …`\n"
            "- Never push to main/master; only work on a feature branch\n"
            "- If an open PR already closes this issue, continue on that PR branch\n"
            "- Stay in scope of this issue; no drive-by refactors\n"
            "- Add/update tests under tests/ when behavior changes\n"
            "- Follow brutal-minimalist brand rules below for any UI work\n"
            "- Live site reference: https://saberistic.com/\n"
            "- Binary assets (PNG/JPEG/WebP) must remain valid binary files\n"
            "- New admin/UI pages MUST include ADMIN_PREVIEW_MODE randomized "
            "mock data (see app/admin_preview.py + AGENTS/builder.md) so "
            "Reviewer screenshots are not empty shells\n"
        )
    return (
        f"You are the Builder agent for `{repo}`.\n"
        f"Implement GitHub issue #{issue}.\n\n"
        f"{ship_rules}\n"
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


def _should_skip(rel: str) -> bool:
    return any(rel.startswith(p) or f"/{p}" in f"/{rel}" for p in SKIP_PATH_PREFIXES)


def _collect_changed_files(root: Path) -> list[tuple[str, str | bytes]]:
    """Return (path, content) for tracked+untracked changes vs HEAD."""
    proc = subprocess.run(
        ["git", "status", "--porcelain", "-u"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise GitHubError(f"git status failed: {proc.stderr.strip()}")
    paths: list[str] = []
    for line in proc.stdout.splitlines():
        if len(line) < 4:
            continue
        entry = line[3:].strip()
        if " -> " in entry:
            entry = entry.split(" -> ", 1)[1].strip()
        entry = entry.strip('"')
        if not entry or _should_skip(entry):
            continue
        paths.append(entry)
    # Also include modified tracked files if porcelain missed somehow
    diff = subprocess.run(
        ["git", "diff", "--name-only", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    for line in (diff.stdout or "").splitlines():
        rel = line.strip()
        if rel and not _should_skip(rel):
            paths.append(rel)

    unique: list[str] = []
    seen: set[str] = set()
    for p in paths:
        if p not in seen:
            seen.add(p)
            unique.append(p)

    from codegen_models import MAX_FILE_CHARS, is_binary_path

    max_files = int(os.environ.get("CURSOR_MAX_FILES") or "30")
    if not unique:
        raise GitHubError("Cursor local agent finished but changed no files")
    if len(unique) > max_files:
        raise GitHubError(
            f"Cursor changed too many files ({len(unique)} > {max_files}): "
            + ", ".join(unique[:20])
        )

    out: list[tuple[str, str | bytes]] = []
    for rel in unique:
        path = root / rel
        if not path.is_file():
            # deleted file — skip for now (Builder Contents API path is put-only)
            continue
        if is_binary_path(rel):
            data = path.read_bytes()
            if len(data) > MAX_FILE_CHARS:
                raise GitHubError(f"file too large after Cursor edit: {rel}")
            out.append((rel, data))
            continue
        text = path.read_text(encoding="utf-8")
        if len(text) > MAX_FILE_CHARS:
            raise GitHubError(f"file too large after Cursor edit: {rel}")
        out.append((rel, text))
    if not out:
        raise GitHubError("Cursor local agent produced no readable file contents")
    return out


def _slugify(text: str, limit: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (slug or "work")[:limit]


def _build_local(
    repo: str,
    issue: int,
    *,
    title: str,
    body: str,
    brief: Path,
    model: str,
    key: str,
) -> dict[str, Any]:
    from cursor_sdk import Agent, CursorAgentError, LocalAgentOptions

    root = Path.cwd()
    prompt = build_prompt(
        repo=repo,
        issue=issue,
        title=title,
        body=body,
        brief=brief,
        runtime="local",
    )
    agent_id = ""
    run_id = ""
    try:
        with Agent.create(
            model=model,
            api_key=key,
            name=f"builder-{issue}",
            local=LocalAgentOptions(cwd=str(root)),
        ) as agent:
            agent_id = str(getattr(agent, "agent_id", "") or "")
            run = agent.send(prompt)
            run_id = str(getattr(run, "id", "") or "")
            result = run.wait()
    except CursorAgentError as exc:
        retryable = getattr(exc, "is_retryable", False)
        raise GitHubError(
            f"Cursor SDK local startup failed (retryable={retryable}): {exc}"
        ) from exc

    status = getattr(result, "status", None)
    if status != "finished":
        raise GitHubError(
            f"Cursor local run did not finish: status={status!r} "
            f"agent_id={getattr(result, 'agent_id', agent_id)} "
            f"run_id={getattr(result, 'id', run_id)} "
            f"result={getattr(result, 'result', '')[:500]!r}"
        )

    files = _collect_changed_files(root)
    owner, name = split_repo(repo)
    default = api("GET", f"/repos/{owner}/{name}").get("default_branch") or "main"
    ref = api("GET", f"/repos/{owner}/{name}/git/ref/heads/{default}")
    base_sha = ref["object"]["sha"]
    from codegen_models import ensure_branch, put_file_batch, resolve_builder_branch

    branch, existing_pr = resolve_builder_branch(repo, issue, title)
    ensure_branch(repo, branch, base_sha)
    commit_message = f"builder(#{issue}): implement via Cursor SDK"
    put_file_batch(repo, branch, files, commit_message)

    if existing_pr:
        pr_number = int(existing_pr["number"])
        created = False
    else:
        pr = api(
            "POST",
            f"/repos/{owner}/{name}/pulls",
            body={
                "title": f"builder: {title} (#{issue})",
                "head": branch,
                "base": default,
                "body": (
                    f"Closes #{issue}\n\n"
                    f"{(getattr(result, 'result', '') or 'Cursor local agent change.')[:2000]}\n\n"
                    "### Codegen\n"
                    "- provider: `cursor`\n"
                    f"- runtime: `local`\n"
                    f"- model: `{model}`\n"
                    f"- agent_id: `{getattr(result, 'agent_id', agent_id)}`\n"
                    f"- run_id: `{getattr(result, 'id', run_id)}`\n"
                    f"- files: {', '.join(f'`{p}`' for p, _ in files)}\n"
                ),
            },
        )
        pr_number = int(pr["number"])
        created = True

    apply_pr_mirror(
        repo,
        issue,
        pr_number,
        default_review="review:needs-review",
    )

    comment = (
        "### builder_result\n"
        "- kind: `cursor`\n"
        "- runtime: `local`\n"
        f"- model: `{model}`\n"
        f"- agent_id: `{getattr(result, 'agent_id', agent_id)}`\n"
        f"- run_id: `{getattr(result, 'id', run_id)}`\n"
        f"- branch: `{branch}`\n"
        f"- pr: #{pr_number}\n"
        f"- files: {', '.join(f'`{p}`' for p, _ in files)}\n"
        f"- created_pr: `{str(created).lower()}`\n"
        f"- summary: {(getattr(result, 'result', '') or '')[:500]}\n"
    )
    post_issue_comment(repo, issue, comment)
    return {
        "provider": "cursor",
        "model": model,
        "runtime": "local",
        "ui_design": False,
        "branch": branch,
        "pr": pr_number,
        "files": [p for p, _ in files],
        "created_pr": created,
        "agent_id": getattr(result, "agent_id", agent_id),
        "run_id": getattr(result, "id", run_id),
    }


def _build_cloud(
    repo: str,
    issue: int,
    *,
    title: str,
    body: str,
    brief: Path,
    model: str,
    key: str,
) -> dict[str, Any]:
    from cursor_sdk import (
        Agent,
        CloudAgentOptions,
        CloudRepository,
        CursorAgentError,
    )

    prompt = build_prompt(
        repo=repo,
        issue=issue,
        title=title,
        body=body,
        brief=brief,
        runtime="cloud",
    )
    repo_url = _repo_https_url(repo)
    owner, name = split_repo(repo)
    default = api("GET", f"/repos/{owner}/{name}").get("default_branch") or "main"
    # Prefer SHA so Cursor does not need to resolve the branch name separately.
    ref = api("GET", f"/repos/{owner}/{name}/git/ref/heads/{default}")
    base_sha = ref["object"]["sha"]

    agent_id = ""
    run_id = ""
    try:
        with Agent.create(
            model=model,
            api_key=key,
            name=f"builder-{issue}",
            cloud=CloudAgentOptions(
                repos=[
                    CloudRepository(
                        url=repo_url,
                        starting_ref=base_sha,
                    )
                ],
                auto_create_pr=True,
                skip_reviewer_request=True,
            ),
        ) as agent:
            agent_id = str(getattr(agent, "agent_id", "") or "")
            run = agent.send(prompt)
            run_id = str(getattr(run, "id", "") or "")
            result = run.wait()
    except CursorAgentError as exc:
        retryable = getattr(exc, "is_retryable", False)
        raise GitHubError(
            "Cursor SDK cloud startup failed "
            f"(retryable={retryable}): {exc}. "
            "Connect GitHub for this Cursor account to saberistic-team/agent-web, "
            "or set CURSOR_RUNTIME=local (default in Actions)."
        ) from exc

    status = getattr(result, "status", None)
    if status != "finished":
        raise GitHubError(
            f"Cursor cloud run did not finish: status={status!r} "
            f"agent_id={getattr(result, 'agent_id', agent_id)} "
            f"run_id={getattr(result, 'id', run_id)} "
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
                "Cursor cloud run finished but no PR was found. "
                f"agent_id={getattr(result, 'agent_id', agent_id)} "
                f"run_id={getattr(result, 'id', run_id)} "
                f"git={git!r}"
            )

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
                        "- provider: `cursor`\n"
                        "- runtime: `cloud`\n"
                        f"- model: `{model}`\n"
                        f"- agent_id: `{getattr(result, 'agent_id', agent_id)}`\n"
                        f"- run_id: `{getattr(result, 'id', run_id)}`\n"
                    )
                },
            )
    except Exception:
        pass

    apply_pr_mirror(
        repo,
        issue,
        pr_number,
        default_review="review:needs-review",
    )

    comment = (
        "### builder_result\n"
        "- kind: `cursor`\n"
        "- runtime: `cloud`\n"
        f"- model: `{model}`\n"
        f"- agent_id: `{getattr(result, 'agent_id', agent_id)}`\n"
        f"- run_id: `{getattr(result, 'id', run_id)}`\n"
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
        "runtime": "cloud",
        "ui_design": False,
        "branch": branch,
        "pr": pr_number,
        "files": [],
        "created_pr": created,
        "agent_id": getattr(result, "agent_id", agent_id),
        "run_id": getattr(result, "id", run_id),
    }


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
        import cursor_sdk  # noqa: F401
    except ImportError as exc:
        raise GitHubError(
            "cursor-sdk is not installed; pip install -r requirements-agents.txt"
        ) from exc

    model = cursor_model()
    runtime = cursor_runtime()
    if runtime == "cloud":
        return _build_cloud(
            repo, issue, title=title, body=body, brief=brief, model=model, key=key
        )
    return _build_local(
        repo, issue, title=title, body=body, brief=brief, model=model, key=key
    )
