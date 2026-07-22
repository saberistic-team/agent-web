#!/usr/bin/env python3
"""Shared GitHub REST helpers for agent-web scripts (stdlib only)."""

from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

# Intentional PR↔issue links only. Bare ``#N`` in prose (e.g. “preview #109”
# on a dependent PR) must NOT bind Builder to that head — learned from
# milestone 4 thrash (#109 commits landing on PR #181 for #110).
_CLOSES_ISSUE_RE = re.compile(
    r"(?i)\b(?:closes|fixes|resolves)\s+#(\d+)\b"
)
_TITLE_ISSUE_RE = re.compile(r"\(#(\d+)\)")

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


def pr_head_ref(pr: dict[str, Any]) -> str:
    return str(((pr.get("head") or {}).get("ref") or "")).strip()


def pr_links_issue(pr: dict[str, Any], issue: int) -> bool:
    """True when the PR intentionally targets ``issue``.

    Counts:
    - head branch ``builder/{issue}-…`` (or exact ``builder/{issue}``)
    - title marker ``(#issue)``
    - ``Closes`` / ``Fixes`` / ``Resolves #issue`` in the body

    Does **not** count casual ``#issue`` mentions in the body (those caused
    Builder to push onto the wrong open PR when dependents referenced an
    earlier issue number).
    """
    n = int(issue)
    head = pr_head_ref(pr)
    if head == f"builder/{n}" or head.startswith(f"builder/{n}-"):
        return True
    title = pr.get("title") or ""
    if any(int(match) == n for match in _TITLE_ISSUE_RE.findall(title)):
        return True
    body = pr.get("body") or ""
    if any(int(match) == n for match in _CLOSES_ISSUE_RE.findall(body)):
        return True
    return False


def _linked_pr_rank(pr: dict[str, Any], issue: int) -> tuple[int, int]:
    """Sort key: stronger intentional links first, then lower PR number."""
    n = int(issue)
    head = pr_head_ref(pr)
    title = pr.get("title") or ""
    body = pr.get("body") or ""
    score = 0
    if head == f"builder/{n}" or head.startswith(f"builder/{n}-"):
        score += 100
    if any(int(match) == n for match in _CLOSES_ISSUE_RE.findall(body)):
        score += 50
    if any(int(match) == n for match in _TITLE_ISSUE_RE.findall(title)):
        score += 25
    return (-score, int(pr.get("number") or 0))


def linked_open_prs(repo: str, issue: int) -> list[dict[str, Any]]:
    """Open PRs that intentionally link ``issue``, strongest binding first."""
    owner, name = split_repo(repo)
    prs = api("GET", f"/repos/{owner}/{name}/pulls?state=open&per_page=100") or []
    matched = [pr for pr in prs if pr_links_issue(pr, issue)]
    matched.sort(key=lambda pr: _linked_pr_rank(pr, issue))
    return matched


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


def put_files(
    repo: str,
    branch: str,
    files: list[tuple[str, bytes]],
    message: str,
) -> str:
    """Commit many path→bytes updates in **one** Git commit on ``branch``.

    Uses the Git Data API (blobs + tree + commit + ref update) instead of
    per-path Contents API PUTs. One Contents PUT = one commit; Builder
    codegen and Reviewer screenshots historically pushed dozens of commits
    and storm CI while racing other merges into dirty PRs (Builder↔Reviewer
    thrash).

    Returns the new commit SHA. Empty ``files`` returns the current HEAD SHA.
    """
    token()
    owner, name = split_repo(repo)
    ref = api("GET", f"/repos/{owner}/{name}/git/ref/heads/{branch}")
    head_sha = str(ref["object"]["sha"])
    if not files:
        return head_sha

    head_commit = api("GET", f"/repos/{owner}/{name}/git/commits/{head_sha}")
    base_tree = str(head_commit["tree"]["sha"])
    tree_items: list[dict[str, str]] = []
    for path, content in files:
        rel = path.lstrip("/")
        if not rel or ".." in rel.split("/"):
            raise GitHubError(f"unsafe path for put_files: {path!r}")
        blob = api(
            "POST",
            f"/repos/{owner}/{name}/git/blobs",
            body={
                "content": base64.b64encode(content).decode("ascii"),
                "encoding": "base64",
            },
        )
        tree_items.append(
            {
                "path": rel,
                "mode": "100644",
                "type": "blob",
                "sha": str(blob["sha"]),
            }
        )

    tree = api(
        "POST",
        f"/repos/{owner}/{name}/git/trees",
        body={"base_tree": base_tree, "tree": tree_items},
    )
    commit = api(
        "POST",
        f"/repos/{owner}/{name}/git/commits",
        body={
            "message": message,
            "tree": tree["sha"],
            "parents": [head_sha],
        },
    )
    new_sha = str(commit["sha"])
    last_err: Exception | None = None
    for attempt in range(4):
        try:
            # Re-read tip when retrying so a racing push (Builder/Reviewer) does
            # not strand screenshot uploads on "Update is not a fast forward".
            if attempt:
                ref = api("GET", f"/repos/{owner}/{name}/git/ref/heads/{branch}")
                head_sha = str(ref["object"]["sha"])
                head_commit = api(
                    "GET", f"/repos/{owner}/{name}/git/commits/{head_sha}"
                )
                base_tree = str(head_commit["tree"]["sha"])
                tree = api(
                    "POST",
                    f"/repos/{owner}/{name}/git/trees",
                    body={"base_tree": base_tree, "tree": tree_items},
                )
                commit = api(
                    "POST",
                    f"/repos/{owner}/{name}/git/commits",
                    body={
                        "message": message,
                        "tree": tree["sha"],
                        "parents": [head_sha],
                    },
                )
                new_sha = str(commit["sha"])
            api(
                "PATCH",
                f"/repos/{owner}/{name}/git/refs/heads/{branch}",
                body={"sha": new_sha},
            )
            return new_sha
        except GitHubError as exc:
            last_err = exc
            if "not a fast forward" not in str(exc).lower() and "422" not in str(exc):
                raise
            time.sleep(0.5 * (attempt + 1))
    assert last_err is not None
    raise last_err


def graphql(
    query: str,
    variables: dict[str, Any] | None = None,
    *,
    token_override: str | None = None,
) -> dict[str, Any]:
    """Minimal GraphQL client (stdlib only) for the handful of mutations the
    REST API cannot express (e.g. enabling PR auto-merge)."""
    payload = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=payload,
        method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token_override or token()}",
            "Content-Type": "application/json",
            "User-Agent": "agent-web",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise GitHubError(f"graphql HTTP {exc.code}: {detail}") from exc
    if data.get("errors"):
        raise GitHubError(f"graphql errors: {data['errors']}")
    return data["data"]


def get_branch_sha(repo: str, branch: str) -> str:
    owner, name = split_repo(repo)
    ref = api("GET", f"/repos/{owner}/{name}/git/ref/heads/{branch}")
    return str(ref["object"]["sha"])


def create_branch(repo: str, branch: str, *, base_branch: str) -> str:
    """Create ``branch`` pointing at the current tip of ``base_branch``.

    Idempotent: if ``branch`` already exists, returns its current sha
    unchanged (a prior automated run may not have merged yet).
    """
    owner, name = split_repo(repo)
    try:
        return get_branch_sha(repo, branch)
    except GitHubError:
        pass
    sha = get_branch_sha(repo, base_branch)
    try:
        api(
            "POST",
            f"/repos/{owner}/{name}/git/refs",
            body={"ref": f"refs/heads/{branch}", "sha": sha},
        )
    except GitHubError as exc:
        if "already exists" not in str(exc).lower():
            raise
    return sha


def find_open_pr_for_branch(repo: str, branch: str) -> dict[str, Any] | None:
    """Return the open PR with head ``branch``, if any (avoids duplicate PRs
    across repeated automated runs, e.g. one per deploy)."""
    owner, name = split_repo(repo)
    results = api(
        "GET",
        f"/repos/{owner}/{name}/pulls?state=open&head={owner}:{urllib.parse.quote(branch)}",
    )
    return results[0] if results else None


def open_pull_request(
    repo: str,
    *,
    head: str,
    base: str,
    title: str,
    body: str,
) -> dict[str, Any]:
    owner, name = split_repo(repo)
    return api(
        "POST",
        f"/repos/{owner}/{name}/pulls",
        body={"head": head, "base": base, "title": title, "body": body},
    )


def enable_auto_merge(
    repo: str,
    pull_request_node_id: str,
    *,
    merge_method: str = "SQUASH",
) -> None:
    """Enable native GitHub auto-merge so the PR merges itself the instant
    branch protection is satisfied (e.g. one human CODEOWNER approval) —
    no REST endpoint exists for this, only GraphQL.
    """
    graphql(
        """
        mutation($pullRequestId: ID!, $mergeMethod: PullRequestMergeMethod!) {
          enablePullRequestAutoMerge(input: {
            pullRequestId: $pullRequestId,
            mergeMethod: $mergeMethod
          }) {
            pullRequest { id }
          }
        }
        """,
        {"pullRequestId": pull_request_node_id, "mergeMethod": merge_method},
    )
