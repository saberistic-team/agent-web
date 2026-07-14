#!/usr/bin/env python3
"""Sync issues/PRs onto the org GitHub Project board from orchestration labels.

Keeps Project Status / Priority / Agent / Review fields aligned with issue
labels (``status:*``, ``priority:*``, ``agent:*``, ``review:*``). Status
workflow remains label-driven; the board Status field is a display mirror for
Kanban only — see docs/LABELS.md.

Env:
  GITHUB_TOKEN / GH_TOKEN — required (use repo secret MODELS_TOKEN in Actions;
    needs classic ``project`` scope for org Projects)
  GITHUB_REPOSITORY — owner/name (default from --repo)
  PROJECT_OWNER — org/user login (default: repo owner)
  PROJECT_NUMBER — project number (default: 8)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

DEFAULT_PROJECT_NUMBER = 8

# Built-in Status (Kanban columns) ← status:* labels
STATUS_BY_LABEL = {
    "status:new": "Todo",
    "status:queued": "Todo",
    "status:in-progress": "In Progress",
    "status:blocked": "Blocked",
    "status:needs-review": "Needs Review",
    "status:done": "Done",
    "status:failed": "Failed",
}

PRIORITY_BY_LABEL = {
    "priority:critical": "critical",
    "priority:high": "high",
    "priority:medium": "medium",
    "priority:normal": "normal",
    "priority:low": "low",
}

AGENT_BY_LABEL = {
    "agent:planner": "planner",
    "agent:builder": "builder",
    "agent:reviewer": "reviewer",
    "agent:docs": "docs",
}

REVIEW_BY_LABEL = {
    "review:needs-review": "needs-review",
    "review:approved": "approved",
    "review:changes-requested": "changes-requested",
}


class ProjectSyncError(RuntimeError):
    pass


def _token() -> str:
    value = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not value:
        raise ProjectSyncError("missing GITHUB_TOKEN")
    return value


def _graphql(query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=payload,
        method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {_token()}",
            "Content-Type": "application/json",
            "User-Agent": "agent-web-project-sync",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ProjectSyncError(f"graphql HTTP {exc.code}: {detail}") from exc
    if data.get("errors"):
        raise ProjectSyncError(f"graphql errors: {data['errors']}")
    return data["data"]


def _rest(method: str, path: str) -> Any:
    url = f"https://api.github.com{path}"
    req = urllib.request.Request(
        url,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {_token()}",
            "User-Agent": "agent-web-project-sync",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            return json.loads(raw.decode("utf-8")) if raw else None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ProjectSyncError(f"{method} {path} -> {exc.code}: {detail}") from exc


def split_repo(repo: str) -> tuple[str, str]:
    if "/" not in repo:
        raise ProjectSyncError(f"repo must be owner/name, got {repo!r}")
    owner, name = repo.split("/", 1)
    return owner, name


def _option_map(field: dict[str, Any]) -> dict[str, str]:
    return {opt["name"]: opt["id"] for opt in field.get("options") or []}


def load_project(owner: str, number: int) -> dict[str, Any]:
    org_data = _graphql(
        """
        query($login: String!, $number: Int!) {
          organization(login: $login) {
            projectV2(number: $number) {
              id
              title
              fields(first: 40) {
                nodes {
                  ... on ProjectV2FieldCommon { id name }
                  ... on ProjectV2SingleSelectField {
                    id
                    name
                    options { id name }
                  }
                }
              }
            }
          }
        }
        """,
        {"login": owner, "number": number},
    )
    project = (org_data.get("organization") or {}).get("projectV2")
    if not project:
        user_data = _graphql(
            """
            query($login: String!, $number: Int!) {
              user(login: $login) {
                projectV2(number: $number) {
                  id
                  title
                  fields(first: 40) {
                    nodes {
                      ... on ProjectV2FieldCommon { id name }
                      ... on ProjectV2SingleSelectField {
                        id
                        name
                        options { id name }
                      }
                    }
                  }
                }
              }
            }
            """,
            {"login": owner, "number": number},
        )
        project = (user_data.get("user") or {}).get("projectV2")
    if not project:
        raise ProjectSyncError(f"project {owner}/{number} not found")
    fields = {
        node["name"]: node for node in project["fields"]["nodes"] if node.get("name")
    }
    return {"id": project["id"], "title": project["title"], "fields": fields}


def node_id_for_issue_or_pr(owner: str, repo: str, number: int) -> tuple[str, str, set[str]]:
    """Return (node_id, kind, label_names). Prefers PR when the number is a PR."""
    data = _graphql(
        """
        query($owner: String!, $name: String!, $number: Int!) {
          repository(owner: $owner, name: $name) {
            issueOrPullRequest(number: $number) {
              __typename
              ... on Issue {
                id
                state
                labels(first: 50) { nodes { name } }
              }
              ... on PullRequest {
                id
                state
                labels(first: 50) { nodes { name } }
              }
            }
          }
        }
        """,
        {"owner": owner, "name": repo, "number": number},
    )
    node = (data.get("repository") or {}).get("issueOrPullRequest")
    if not node:
        raise ProjectSyncError(f"{owner}/{repo}#{number} not found")
    labels = {n["name"] for n in (node.get("labels") or {}).get("nodes") or []}
    kind = "pull_request" if node["__typename"] == "PullRequest" else "issue"
    return node["id"], kind, labels


def ensure_item(project_id: str, content_id: str) -> str:
    """Add content to the project; returns existing item id when already present."""
    data = _graphql(
        """
        mutation($project: ID!, $content: ID!) {
          addProjectV2ItemById(input: {projectId: $project, contentId: $content}) {
            item { id }
          }
        }
        """,
        {"project": project_id, "content": content_id},
    )
    return data["addProjectV2ItemById"]["item"]["id"]


def set_single_select(
    project_id: str,
    item_id: str,
    field_id: str,
    option_id: str,
) -> None:
    _graphql(
        """
        mutation($project: ID!, $item: ID!, $field: ID!, $option: String!) {
          updateProjectV2ItemFieldValue(
            input: {
              projectId: $project
              itemId: $item
              fieldId: $field
              value: { singleSelectOptionId: $option }
            }
          ) {
            projectV2Item { id }
          }
        }
        """,
        {
            "project": project_id,
            "item": item_id,
            "field": field_id,
            "option": option_id,
        },
    )


def clear_field(project_id: str, item_id: str, field_id: str) -> None:
    _graphql(
        """
        mutation($project: ID!, $item: ID!, $field: ID!) {
          clearProjectV2ItemFieldValue(
            input: { projectId: $project, itemId: $item, fieldId: $field }
          ) {
            projectV2Item { id }
          }
        }
        """,
        {"project": project_id, "item": item_id, "field": field_id},
    )


def _first_mapped(labels: set[str], mapping: dict[str, str]) -> str | None:
    for label, value in mapping.items():
        if label in labels:
            return value
    return None


def apply_fields(
    project: dict[str, Any],
    item_id: str,
    labels: set[str],
    *,
    kind: str,
    closed: bool = False,
) -> dict[str, str | None]:
    fields = project["fields"]
    project_id = project["id"]
    applied: dict[str, str | None] = {}

    # Issues own orchestration status; closed issues without status:done still → Done.
    status_name = _first_mapped(labels, STATUS_BY_LABEL)
    if status_name is None and (closed or "status:done" in labels):
        status_name = "Done"
    if status_name is None and kind == "pull_request":
        # PRs: open → In Progress, closed/merged without mirrors → Done when closed.
        status_name = "Done" if closed else "In Progress"
        if "review:needs-review" in labels:
            status_name = "Needs Review"
        if "review:changes-requested" in labels:
            status_name = "Blocked"
        if "review:approved" in labels and not closed:
            status_name = "Needs Review"

    status_field = fields.get("Status")
    if status_field and status_name:
        opt = _option_map(status_field).get(status_name)
        if opt:
            set_single_select(project_id, item_id, status_field["id"], opt)
            applied["Status"] = status_name

    for field_name, mapping in (
        ("Priority", PRIORITY_BY_LABEL),
        ("Agent", AGENT_BY_LABEL),
        ("Review", REVIEW_BY_LABEL),
    ):
        field = fields.get(field_name)
        if not field:
            continue
        # agent:* / status:* must not live on PRs; still allow Priority/Review mirrors.
        if kind == "pull_request" and field_name == "Agent":
            clear_field(project_id, item_id, field["id"])
            applied[field_name] = None
            continue
        value = _first_mapped(labels, mapping)
        if value is None:
            continue
        opt = _option_map(field).get(value)
        if opt:
            set_single_select(project_id, item_id, field["id"], opt)
            applied[field_name] = value

    return applied


def content_state(owner: str, repo: str, number: int) -> bool:
    """True if issue/PR is closed (or PR merged)."""
    data = _rest("GET", f"/repos/{owner}/{repo}/issues/{number}")
    return (data or {}).get("state") == "closed"


def linked_open_prs(repo: str, issue: int) -> list[int]:
    """Open PRs whose title/body reference ``#issue`` (Builder convention)."""
    owner, name = split_repo(repo)
    prs = _rest("GET", f"/repos/{owner}/{name}/pulls?state=open&per_page=50") or []
    needle = f"#{issue}"
    closes = f"Closes #{issue}"
    out: list[int] = []
    for pr in prs:
        title = pr.get("title") or ""
        body = pr.get("body") or ""
        if needle in title or needle in body or closes.lower() in body.lower():
            out.append(int(pr["number"]))
    return out


def sync_number(
    repo: str,
    number: int,
    *,
    owner: str | None = None,
    project_number: int = DEFAULT_PROJECT_NUMBER,
    project: dict[str, Any] | None = None,
    sync_linked_prs: bool = True,
) -> list[dict[str, Any]]:
    repo_owner, repo_name = split_repo(repo)
    project_owner = owner or os.environ.get("PROJECT_OWNER") or repo_owner
    project_number = int(os.environ.get("PROJECT_NUMBER") or project_number)

    if project is None:
        project = load_project(project_owner, project_number)
    content_id, kind, labels = node_id_for_issue_or_pr(repo_owner, repo_name, number)
    item_id = ensure_item(project["id"], content_id)
    closed = content_state(repo_owner, repo_name, number)
    applied = apply_fields(project, item_id, labels, kind=kind, closed=closed)
    results = [
        {
            "project": project["title"],
            "project_id": project["id"],
            "item_id": item_id,
            "number": number,
            "kind": kind,
            "closed": closed,
            "labels": sorted(labels),
            "fields": applied,
        }
    ]
    if sync_linked_prs and kind == "issue":
        for pr in linked_open_prs(repo, number):
            results.extend(
                sync_number(
                    repo,
                    pr,
                    owner=project_owner,
                    project_number=project_number,
                    project=project,
                    sync_linked_prs=False,
                )
            )
    return results


def sync_many(
    repo: str,
    numbers: list[int],
    *,
    owner: str | None = None,
    project_number: int = DEFAULT_PROJECT_NUMBER,
) -> list[dict[str, Any]]:
    project_owner = owner or os.environ.get("PROJECT_OWNER") or split_repo(repo)[0]
    project = load_project(project_owner, project_number)
    results: list[dict[str, Any]] = []
    seen: set[int] = set()
    for number in numbers:
        if number in seen:
            continue
        for row in sync_number(
            repo,
            number,
            owner=project_owner,
            project_number=project_number,
            project=project,
        ):
            if row["number"] in seen:
                continue
            seen.add(row["number"])
            results.append(row)
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY"),
        help="owner/name (default: GITHUB_REPOSITORY)",
    )
    parser.add_argument(
        "--owner",
        default=os.environ.get("PROJECT_OWNER"),
        help="Project owner login (default: repo owner)",
    )
    parser.add_argument(
        "--project-number",
        type=int,
        default=int(os.environ.get("PROJECT_NUMBER") or DEFAULT_PROJECT_NUMBER),
    )
    parser.add_argument(
        "--issue",
        type=int,
        action="append",
        default=[],
        help="Issue or PR number (repeatable)",
    )
    parser.add_argument(
        "--numbers",
        default="",
        help="Comma-separated issue/PR numbers",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print JSON result",
    )
    args = parser.parse_args(argv)
    if not args.repo:
        print("error: --repo or GITHUB_REPOSITORY required", file=sys.stderr)
        return 2

    numbers = list(args.issue or [])
    if args.numbers.strip():
        numbers.extend(
            int(part.strip()) for part in args.numbers.split(",") if part.strip()
        )
    numbers = sorted(set(numbers))
    if not numbers:
        print("error: pass --issue N and/or --numbers 1,2,3", file=sys.stderr)
        return 2

    results = sync_many(
        args.repo,
        numbers,
        owner=args.owner,
        project_number=args.project_number,
    )
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for row in results:
            fields = ", ".join(
                f"{k}={v}" for k, v in (row["fields"] or {}).items() if v
            )
            print(
                f"#{row['number']} ({row['kind']}) → item {row['item_id']}"
                + (f" [{fields}]" if fields else "")
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
