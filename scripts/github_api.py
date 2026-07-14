#!/usr/bin/env python3
"""Shared GitHub REST helpers for agent-web scripts (stdlib only)."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

# Transient GitHub / network failures Builder (and other roles) hit during codegen.
# Retry a few times with exponential backoff before escalating to @human-review.
RETRYABLE_HTTP_STATUS = frozenset({408, 429, 500, 502, 503, 504})
API_MAX_ATTEMPTS = 5
API_BACKOFF_BASE_S = 1.0
API_BACKOFF_CAP_S = 30.0


class GitHubError(RuntimeError):
    pass


def token() -> str:
    value = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not value:
        raise GitHubError("missing GITHUB_TOKEN")
    return value


def comment_token() -> str:
    """Prefer COMMENT_TOKEN (role App) so audit comments attribute to the bot."""
    return os.environ.get("COMMENT_TOKEN") or token()


def split_repo(repo: str) -> tuple[str, str]:
    if "/" not in repo:
        raise GitHubError(f"repo must be owner/name, got {repo!r}")
    owner, name = repo.split("/", 1)
    return owner, name


def _retry_delay_s(attempt: int, *, retry_after: str | None = None) -> float:
    """Exponential backoff: 1s, 2s, 4s, 8s… capped; honor Retry-After when present."""
    if retry_after:
        try:
            return min(float(retry_after.strip()), API_BACKOFF_CAP_S)
        except ValueError:
            pass
    return min(API_BACKOFF_BASE_S * (2**attempt), API_BACKOFF_CAP_S)


def api(
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    token_override: str | None = None,
) -> Any:
    url = path if path.startswith("http") else f"https://api.github.com{path}"
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token_override or token()}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "agent-web",
        **({"Content-Type": "application/json"} if body is not None else {}),
    }
    last_error: Exception | None = None
    for attempt in range(API_MAX_ATTEMPTS):
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read()
                if not raw:
                    return None
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            last_error = GitHubError(f"{method} {path} -> {exc.code}: {detail}")
            if exc.code not in RETRYABLE_HTTP_STATUS or attempt >= API_MAX_ATTEMPTS - 1:
                raise last_error from exc
            delay = _retry_delay_s(
                attempt, retry_after=exc.headers.get("Retry-After") if exc.headers else None
            )
            time.sleep(delay)
        except (TimeoutError, urllib.error.URLError, OSError) as exc:
            last_error = GitHubError(f"{method} {path} -> transient: {exc}")
            if attempt >= API_MAX_ATTEMPTS - 1:
                raise last_error from exc
            time.sleep(_retry_delay_s(attempt))
    raise last_error or GitHubError(f"{method} {path} -> exhausted retries")


def list_pr_files(repo: str, pr_number: int) -> list[dict[str, Any]]:
    """Paginate pull request files (default API page is only 30)."""
    owner, name = split_repo(repo)
    files: list[dict[str, Any]] = []
    page = 1
    while page <= 20:
        batch = (
            api(
                "GET",
                f"/repos/{owner}/{name}/pulls/{pr_number}/files?per_page=100&page={page}",
            )
            or []
        )
        files.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return files


def post_issue_comment(repo: str, issue: int, body: str) -> dict[str, Any]:
    owner, name = split_repo(repo)
    return api(
        "POST",
        f"/repos/{owner}/{name}/issues/{issue}/comments",
        body={"body": body},
        token_override=comment_token(),
    )


def add_labels(repo: str, issue: int, labels: list[str]) -> Any:
    owner, name = split_repo(repo)
    return api(
        "POST",
        f"/repos/{owner}/{name}/issues/{issue}/labels",
        body={"labels": labels},
    )


def delete_label(repo: str, issue: int, label: str) -> None:
    owner, name = split_repo(repo)
    encoded = urllib.parse.quote(label, safe="")
    try:
        api("DELETE", f"/repos/{owner}/{name}/issues/{issue}/labels/{encoded}")
    except GitHubError:
        pass


def list_issue_comments(repo: str, issue: int) -> list[dict[str, Any]]:
    """Paginate issue comments (default page is 30; busy issues exceed one page)."""
    owner, name = split_repo(repo)
    comments: list[dict[str, Any]] = []
    page = 1
    while page <= 50:
        batch = (
            api(
                "GET",
                f"/repos/{owner}/{name}/issues/{issue}/comments?per_page=100&page={page}",
            )
            or []
        )
        comments.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return comments
