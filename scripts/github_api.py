#!/usr/bin/env python3
"""Shared GitHub REST helpers for agent-web scripts (stdlib only)."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


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


def api(
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    token_override: str | None = None,
) -> Any:
    url = path if path.startswith("http") else f"https://api.github.com{path}"
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token_override or token()}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "agent-web",
            **({"Content-Type": "application/json"} if body is not None else {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            if not raw:
                return None
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise GitHubError(f"{method} {path} -> {exc.code}: {detail}") from exc


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
    owner, name = split_repo(repo)
    return api("GET", f"/repos/{owner}/{name}/issues/{issue}/comments?per_page=100") or []
