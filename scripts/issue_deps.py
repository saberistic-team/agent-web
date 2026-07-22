#!/usr/bin/env python3
"""Issue dependency guards (learned from #204 premature merge).

Machine-readable dependencies are required before Planner queues work and
before Dispatcher / Builder / Docs / Reviewer / Gate proceed:

- GitHub ``blockedBy`` / ``blocking`` relationships (preferred)
- GitHub parent / sub-issue links
- Explicit body lines: ``Depends on: #199, #200`` / ``Blocked by: #199``
- Issue refs inside a ``## Dependencies`` / ``## Dependency`` section

Planner and Dispatcher call ``reconcile_issue_dependencies`` so missing
GitHub links are derived from the issue body, linked PRs, and parent/child
relationships, then written back before queue / dequeue decisions.

A non-empty ``## Dependencies`` section with **no** parseable issue numbers is
treated as unstructured and fail-closed (Planner must rewrite to ``Depends on:``
or mark ``None`` / ``N/A``).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any

from github_api import GitHubError, api, graphql, linked_open_prs, split_repo

DEPENDS_ON_LINE_RE = re.compile(
    r"(?im)^(?:depends\s+on|blocked\s+by|blockers?)\s*:?\s*(.+)$"
)
ISSUE_REF_RE = re.compile(
    r"(?:https://github\.com/[^/\s]+/[^/\s]+/issues/|#)(\d+)\b"
)
DEPENDENCIES_SECTION_RE = re.compile(
    r"(?is)##\s*dependenc(?:y|ies)\s*\n(.*?)(?=\n##\s|\Z)"
)
_NONE_RE = re.compile(r"(?is)^\s*(?:none|n/?a|no\s+dependencies?)\s*\.?\s*$")
_PROVISIONAL_OK_RE = re.compile(
    r"(?is)\b(?:provisional\s+(?:ok|allowed)|deps?\s+exempt|dependency\s+exempt)\b"
)
# Strong ordering phrases that mention an issue number nearby.
_INFER_DEP_RE = re.compile(
    r"(?is)(?:"
    r"depends\s+on|blocked\s+by|blockers?|requires|prerequisite|"
    r"complete\s+after|start\s+only\s+after|blocked\s+until|"
    r"must\s+wait\s+(?:for|until)|after\s+completing|"
    r"use(?:s|ing)?\s+(?:the\s+)?.{0,80}?\s+from|"
    r"produced\s+by|from\s+the\s+earlier"
    r").{0,120}?(?:https://github\.com/[^/\s]+/[^/\s]+/issues/|#)(\d+)\b"
    r"|"
    r"(?:https://github\.com/[^/\s]+/[^/\s]+/issues/|#)(\d+)\b.{0,60}?"
    r"(?:must|should)\s+(?:land|merge|close|complete|ship)\s+first"
)
_PARENT_LINE_RE = re.compile(
    r"(?im)^(?:parent|parent\s+issue|child\s+of|sub-?issue\s+of)\s*:?\s*.{0,40}?"
    r"(?:https://github\.com/[^/\s]+/[^/\s]+/issues/|#)(\d+)\b"
)
_CHILD_OF_COMMENT_RE = re.compile(
    r"(?i)(?:child\s+of|queued\s+as\s+one-commit\s+child\s+of)\s+#(\d+)"
)


def dependency_section(body: str) -> str | None:
    """Return the ``## Dependencies`` / ``## Dependency`` section body, if any."""
    match = DEPENDENCIES_SECTION_RE.search(body or "")
    if not match:
        return None
    return match.group(1).strip()


def parse_dependency_issue_numbers(body: str) -> list[int]:
    """Parse machine-readable dependency issue numbers from an issue body."""
    text = body or ""
    found: list[int] = []
    seen: set[int] = set()

    def _add(raw: str) -> None:
        for match in ISSUE_REF_RE.finditer(raw):
            number = int(match.group(1))
            if number not in seen:
                seen.add(number)
                found.append(number)

    for match in DEPENDS_ON_LINE_RE.finditer(text):
        value = match.group(1).strip()
        if _NONE_RE.match(value):
            continue
        _add(value)

    section = dependency_section(text)
    if section and not _NONE_RE.match(section):
        _add(section)

    return found


def infer_dependency_issue_numbers(body: str) -> list[int]:
    """Infer dependency issue numbers from strong ordering phrases in prose."""
    found: list[int] = []
    seen: set[int] = set(parse_dependency_issue_numbers(body))
    for match in _INFER_DEP_RE.finditer(body or ""):
        raw = match.group(1) or match.group(2)
        if not raw:
            continue
        number = int(raw)
        if number in seen:
            continue
        seen.add(number)
        found.append(number)
    return found


def parse_parent_issue_number(body: str) -> int | None:
    """Return an explicit parent issue number from the body, if any."""
    match = _PARENT_LINE_RE.search(body or "")
    if match:
        return int(match.group(1))
    return None


def has_unstructured_dependencies(body: str) -> bool:
    """True when a Dependencies section has prose but no issue refs.

    Explicit ``None`` / ``N/A`` / ``no dependencies`` is fine. A provisional
    exemption phrase also clears the unstructured check (still prefer
    ``Depends on:`` when real blockers exist).
    """
    section = dependency_section(body or "")
    if section is None:
        return False
    if _NONE_RE.match(section):
        return False
    if _PROVISIONAL_OK_RE.search(section):
        return False
    if parse_dependency_issue_numbers(f"## Dependencies\n{section}\n"):
        return False
    if infer_dependency_issue_numbers(f"## Dependencies\n{section}\n"):
        return False
    # Non-empty prose without refs — fail closed (#204).
    return len(section) >= 12


def fetch_issue_relationships(repo: str, issue: int) -> dict[str, Any]:
    """Load GitHub blockedBy/blocking/parent/subIssues plus body for ``issue``."""
    owner, name = split_repo(repo)
    data = graphql(
        """
        query($owner: String!, $name: String!, $number: Int!) {
          repository(owner: $owner, name: $name) {
            issue(number: $number) {
              id
              number
              title
              state
              body
              parent { number title state id }
              blockedBy(first: 30) {
                nodes { id number title state }
              }
              blocking(first: 30) {
                nodes { id number title state }
              }
              subIssues(first: 30) {
                nodes { id number title state }
              }
            }
          }
        }
        """,
        {"owner": owner, "name": name, "number": int(issue)},
    )
    issue_data = ((data or {}).get("repository") or {}).get("issue")
    if not issue_data:
        raise GitHubError(f"issue #{issue} not found in {repo}")
    return issue_data


def fetch_github_blocked_by(repo: str, issue: int) -> list[dict[str, Any]]:
    """Return GitHub ``blockedBy`` nodes for ``issue`` (may be empty)."""
    try:
        rel = fetch_issue_relationships(repo, issue)
    except GitHubError:
        return []
    nodes = ((rel.get("blockedBy") or {}).get("nodes")) or []
    return [node for node in nodes if node and node.get("number") is not None]


def _issue_state(repo: str, number: int) -> dict[str, Any]:
    owner, name = split_repo(repo)
    data = api("GET", f"/repos/{owner}/{name}/issues/{int(number)}") or {}
    return {
        "number": int(number),
        "title": data.get("title") or "",
        "state": (data.get("state") or "open").lower(),
        "id": data.get("node_id"),
        "body": data.get("body") or "",
    }


def _node_state(node: dict[str, Any]) -> str:
    return str(node.get("state") or "OPEN").lower()


def _issue_node_id(repo: str, number: int) -> str:
    rel = fetch_issue_relationships(repo, number)
    node_id = rel.get("id")
    if not node_id:
        raise GitHubError(f"missing node id for #{number}")
    return str(node_id)


def add_blocked_by_link(repo: str, issue: int, blocking_issue: int) -> str:
    """Ensure ``issue`` is blocked by ``blocking_issue``. Returns ok|exists|fail."""
    if int(issue) == int(blocking_issue):
        return "skip_self"
    try:
        graphql(
            """
            mutation($input: AddBlockedByInput!) {
              addBlockedBy(input: $input) { issue { number } }
            }
            """,
            {
                "input": {
                    "issueId": _issue_node_id(repo, issue),
                    "blockingIssueId": _issue_node_id(repo, blocking_issue),
                }
            },
        )
        return "ok"
    except GitHubError as exc:
        msg = str(exc).lower()
        if "already been taken" in msg or "already" in msg:
            return "exists"
        return f"fail:{exc}"


def add_sub_issue_link(repo: str, parent: int, child: int) -> str:
    """Ensure ``child`` is a GitHub sub-issue of ``parent``."""
    if int(parent) == int(child):
        return "skip_self"
    try:
        graphql(
            """
            mutation($input: AddSubIssueInput!) {
              addSubIssue(input: $input) {
                issue { number }
                subIssue { number }
              }
            }
            """,
            {
                "input": {
                    "issueId": _issue_node_id(repo, parent),
                    "subIssueId": _issue_node_id(repo, child),
                    "replaceParent": False,
                }
            },
        )
        return "ok"
    except GitHubError as exc:
        msg = str(exc).lower()
        if "already" in msg or "duplicate" in msg:
            return "exists"
        return f"fail:{exc}"


def sync_depends_on_body(repo: str, issue: int, blockers: list[int], body: str) -> str:
    """Rewrite/insert a machine-readable Depends on line; return updated body."""
    blockers = sorted({int(n) for n in blockers if int(n) != int(issue)})
    if not blockers:
        return body
    deps_line = "Depends on: " + ", ".join(f"#{n}" for n in blockers)
    section = (
        "## Dependencies\n\n"
        f"{deps_line}\n\n"
        "Derived/maintained for dispatcher and reviewer "
        "(`scripts/issue_deps.py`).\n"
    )
    text = body or ""
    if re.search(r"(?im)^##\s*dependenc(?:y|ies)\s*$", text):
        updated, count = re.subn(
            r"(?is)##\s*dependenc(?:y|ies)\s*\n.*?(?=\n##\s|\Z)",
            section.rstrip() + "\n\n",
            text,
            count=1,
        )
        if count:
            return updated
    if DEPENDS_ON_LINE_RE.search(text):
        return DEPENDS_ON_LINE_RE.sub(deps_line, text, count=1)
    match = re.search(r"(?im)^##\s*acceptance criteria\s*$", text)
    if match:
        return text[: match.start()] + section + "\n" + text[match.start() :]
    return text.rstrip() + "\n\n" + section


def _patch_issue_body(repo: str, issue: int, body: str) -> None:
    owner, name = split_repo(repo)
    api(
        "PATCH",
        f"/repos/{owner}/{name}/issues/{int(issue)}",
        body={"body": body},
    )


def _collect_pr_inferred_deps(repo: str, issue: int) -> list[int]:
    """Infer dependency numbers from intentionally linked open PR bodies/titles."""
    found: list[int] = []
    seen: set[int] = set()
    try:
        prs = linked_open_prs(repo, issue)
    except GitHubError:
        return []
    for pr in prs:
        blob = f"{pr.get('title') or ''}\n{pr.get('body') or ''}"
        for number in parse_dependency_issue_numbers(blob) + infer_dependency_issue_numbers(
            blob
        ):
            if number == int(issue) or number in seen:
                continue
            seen.add(number)
            found.append(number)
    return found


def _parent_from_planner_comments(repo: str, issue: int) -> int | None:
    """Detect parent from Planner child comments when GraphQL parent is missing."""
    owner, name = split_repo(repo)
    comments = (
        api(
            "GET",
            f"/repos/{owner}/{name}/issues/{int(issue)}/comments?per_page=30",
        )
        or []
    )
    for comment in comments:
        body = comment.get("body") or ""
        match = _CHILD_OF_COMMENT_RE.search(body)
        if match:
            return int(match.group(1))
    return None


def desired_blockers(
    repo: str,
    issue: int,
    *,
    rel: dict[str, Any] | None = None,
    body: str | None = None,
) -> list[dict[str, Any]]:
    """Compute desired blocker issues (open + closed) with sources."""
    rel = rel or fetch_issue_relationships(repo, issue)
    text = body if body is not None else (rel.get("body") or "")
    desired: dict[int, dict[str, Any]] = {}

    def _remember(number: int, source: str, meta: dict[str, Any] | None = None) -> None:
        if int(number) == int(issue):
            return
        existing = desired.get(int(number))
        if existing:
            sources = set(existing.get("sources") or [])
            sources.add(source)
            existing["sources"] = sorted(sources)
            return
        info = dict(meta or {})
        info.setdefault("number", int(number))
        info["sources"] = [source]
        desired[int(number)] = info

    for node in ((rel.get("blockedBy") or {}).get("nodes")) or []:
        _remember(int(node["number"]), "blocked_by", node)

    for number in parse_dependency_issue_numbers(text):
        _remember(number, "body")

    for number in infer_dependency_issue_numbers(text):
        _remember(number, "inferred_body")

    for number in _collect_pr_inferred_deps(repo, issue):
        _remember(number, "inferred_pr")

    # Parent with open children must not dequeue until children finish.
    for node in ((rel.get("subIssues") or {}).get("nodes")) or []:
        if _node_state(node) == "open":
            _remember(int(node["number"]), "open_sub_issue", node)

    # Fill missing titles/states for body-only refs.
    for number, info in list(desired.items()):
        if info.get("title") and info.get("state"):
            continue
        try:
            meta = _issue_state(repo, number)
        except GitHubError:
            continue
        info.setdefault("title", meta.get("title") or "")
        info.setdefault("state", meta.get("state") or "open")
        if meta.get("id"):
            info.setdefault("id", meta["id"])

    return sorted(desired.values(), key=lambda item: int(item["number"]))


def reconcile_issue_dependencies(
    repo: str,
    issue: int,
    *,
    body: str | None = None,
    write: bool = True,
) -> dict[str, Any]:
    """Derive missing deps, optionally write GitHub links + body, return summary.

    Always considers blockedBy/blocking/parent/subIssues. When ``write`` is true,
    adds missing ``blockedBy`` and parent/child links and syncs ``Depends on:``.
    """
    try:
        rel = fetch_issue_relationships(repo, issue)
    except GitHubError as exc:
        return {
            "issue": int(issue),
            "ok": False,
            "error": str(exc),
            "blockers": [],
            "added_blocked_by": [],
            "added_sub_issues": [],
            "body_updated": False,
        }

    text = body if body is not None else (rel.get("body") or "")
    desired = desired_blockers(repo, issue, rel=rel, body=text)
    existing_bb = {
        int(n["number"])
        for n in ((rel.get("blockedBy") or {}).get("nodes") or [])
        if n.get("number") is not None
    }
    added_bb: list[dict[str, Any]] = []
    added_sub: list[dict[str, Any]] = []
    body_updated = False

    # Parent/child: prefer GraphQL parent, else body/planner comment.
    parent = rel.get("parent") or {}
    parent_number = int(parent["number"]) if parent.get("number") else None
    if parent_number is None:
        parent_number = parse_parent_issue_number(text) or _parent_from_planner_comments(
            repo, issue
        )
        if write and parent_number is not None:
            status = add_sub_issue_link(repo, parent_number, issue)
            added_sub.append(
                {"parent": parent_number, "child": int(issue), "status": status}
            )

    # Ensure each child spawned under this issue is linked (planner path).
    # No-op when subIssues already present.

    open_blockers: list[dict[str, Any]] = []
    for item in desired:
        number = int(item["number"])
        state = str(item.get("state") or "open").lower()
        if write and number not in existing_bb:
            # Only create blockedBy for issues that still matter (open) or were
            # explicitly declared — still link open ones; skip closed to avoid noise.
            if state == "open":
                status = add_blocked_by_link(repo, issue, number)
                added_bb.append({"blocking": number, "status": status})
                if status in {"ok", "exists"}:
                    existing_bb.add(number)
        if state == "open":
            open_blockers.append(
                {
                    "number": number,
                    "title": item.get("title") or "",
                    "state": "open",
                    "source": ",".join(item.get("sources") or []),
                }
            )

    if write:
        # Prefer declared+inferred open blockers + existing blockedBy for the body.
        body_nums = sorted({int(b["number"]) for b in open_blockers} | existing_bb)
        new_body = sync_depends_on_body(repo, issue, body_nums, text)
        if new_body != text and body_nums:
            _patch_issue_body(repo, issue, new_body)
            body_updated = True
            text = new_body

    return {
        "issue": int(issue),
        "ok": True,
        "parent": parent_number,
        "desired": desired,
        "blockers": open_blockers,
        "added_blocked_by": added_bb,
        "added_sub_issues": added_sub,
        "body_updated": body_updated,
        "unstructured": has_unstructured_dependencies(text),
        "body": text,
    }


def open_dependency_blockers(
    repo: str,
    issue: int,
    *,
    body: str | None = None,
    reconcile: bool = False,
) -> list[dict[str, Any]]:
    """Return still-open blockers for ``issue``.

    When ``reconcile`` is true, derive/write missing GitHub relationships first.
    """
    if reconcile:
        summary = reconcile_issue_dependencies(repo, issue, body=body, write=True)
        return list(summary.get("blockers") or [])

    if body is None:
        owner, name = split_repo(repo)
        issue_data = api("GET", f"/repos/{owner}/{name}/issues/{int(issue)}") or {}
        body = issue_data.get("body") or ""

    blockers: dict[int, dict[str, Any]] = {}

    try:
        for node in fetch_github_blocked_by(repo, issue):
            number = int(node["number"])
            if _node_state(node) != "open":
                continue
            blockers[number] = {
                "number": number,
                "title": node.get("title") or "",
                "state": "open",
                "source": "blocked_by",
            }
    except GitHubError:
        pass

    # Also treat open sub-issues as blockers for a parent issue.
    try:
        rel = fetch_issue_relationships(repo, issue)
        for node in ((rel.get("subIssues") or {}).get("nodes")) or []:
            if _node_state(node) != "open":
                continue
            number = int(node["number"])
            blockers[number] = {
                "number": number,
                "title": node.get("title") or "",
                "state": "open",
                "source": "open_sub_issue",
            }
    except GitHubError:
        pass

    for number in parse_dependency_issue_numbers(body or "") + infer_dependency_issue_numbers(
        body or ""
    ):
        if number == int(issue) or number in blockers:
            continue
        meta = _issue_state(repo, number)
        if meta["state"] != "open":
            continue
        blockers[number] = {**meta, "source": "body"}

    return sorted(blockers.values(), key=lambda item: int(item["number"]))


def format_blocker_refs(blockers: list[dict[str, Any]]) -> str:
    if not blockers:
        return "(none)"
    return ", ".join(f"#{int(b['number'])}" for b in blockers)


def dependency_block_reason(
    repo: str,
    issue: int,
    *,
    body: str | None = None,
    reconcile: bool = False,
) -> str | None:
    """Human-readable reason when dependencies are unmet, else ``None``."""
    summary: dict[str, Any] | None = None
    if reconcile:
        summary = reconcile_issue_dependencies(repo, issue, body=body, write=True)
        body = summary.get("body") or body
        if summary.get("unstructured"):
            return (
                f"#{issue} has a ## Dependencies section without machine-readable "
                "issue refs. Rewrite as `Depends on: #N, #M` (or `None` / `N/A`), "
                "or set GitHub blockedBy links — do not invent stand-in schemas "
                "(learned from #204)."
            )
        blockers = list(summary.get("blockers") or [])
    else:
        if body is None:
            owner, name = split_repo(repo)
            issue_data = api("GET", f"/repos/{owner}/{name}/issues/{int(issue)}") or {}
            body = issue_data.get("body") or ""
        if has_unstructured_dependencies(body or ""):
            return (
                f"#{issue} has a ## Dependencies section without machine-readable "
                "issue refs. Rewrite as `Depends on: #N, #M` (or `None` / `N/A`), "
                "or set GitHub blockedBy links — do not invent stand-in schemas "
                "(learned from #204)."
            )
        blockers = open_dependency_blockers(repo, issue, body=body, reconcile=False)

    if not blockers:
        return None
    refs = format_blocker_refs(blockers)
    titles = "; ".join(
        f"#{int(b['number'])} {b.get('title') or ''}".strip() for b in blockers
    )
    return (
        f"#{issue} is blocked by open dependencies: {refs}. "
        f"Wait until they close (or remove the Depends-on / blockedBy link). "
        f"Details: {titles}"
    )


def require_dependencies_met(
    repo: str,
    issue: int,
    *,
    body: str | None = None,
    reconcile: bool = False,
) -> None:
    """Raise ``GitHubError`` when open or unstructured dependencies remain."""
    reason = dependency_block_reason(repo, issue, body=body, reconcile=reconcile)
    if reason:
        raise GitHubError(reason)


def dispatcher_skip_comment(issue: int, reason: str) -> str:
    return (
        "### dispatcher_skip\n"
        f"- issue: `#{issue}`\n"
        "- reason: `open_dependencies`\n"
        f"- detail: {reason}\n"
    )


def reconcile_comment(summary: dict[str, Any]) -> str | None:
    """Build an audit comment when reconcile wrote GitHub links / body."""
    added_bb = [
        item
        for item in (summary.get("added_blocked_by") or [])
        if item.get("status") == "ok"
    ]
    added_sub = [
        item
        for item in (summary.get("added_sub_issues") or [])
        if item.get("status") == "ok"
    ]
    if not added_bb and not added_sub and not summary.get("body_updated"):
        return None
    lines = [
        "### dependency_reconcile",
        f"- issue: `#{summary.get('issue')}`",
    ]
    if added_bb:
        refs = ", ".join(f"#{int(i['blocking'])}" for i in added_bb)
        lines.append(f"- added_blocked_by: {refs}")
    if added_sub:
        for item in added_sub:
            lines.append(
                f"- added_sub_issue: `#{item['child']}` under `#{item['parent']}`"
            )
    if summary.get("body_updated"):
        lines.append("- body: synced `Depends on:`")
    blockers = summary.get("blockers") or []
    if blockers:
        lines.append(f"- open_blockers: {format_blocker_refs(blockers)}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--issue", type=int, required=True)
    parser.add_argument(
        "--mode",
        choices=("check", "require", "reconcile"),
        default="check",
        help="check prints JSON; require exits non-zero when blocked; "
        "reconcile writes missing GitHub links then prints JSON",
    )
    args = parser.parse_args(argv)

    try:
        if args.mode == "reconcile":
            summary = reconcile_issue_dependencies(
                args.repo, args.issue, write=True
            )
            print(json.dumps(summary, sort_keys=True, indent=2, default=str))
            return 0 if summary.get("ok") else 1

        owner, name = split_repo(args.repo)
        issue_data = (
            api("GET", f"/repos/{owner}/{name}/issues/{int(args.issue)}") or {}
        )
        body = issue_data.get("body") or ""
        reason = dependency_block_reason(args.repo, args.issue, body=body)
        blockers = open_dependency_blockers(args.repo, args.issue, body=body)
        payload = {
            "issue": args.issue,
            "ok": reason is None,
            "unstructured": has_unstructured_dependencies(body),
            "blockers": blockers,
            "reason": reason,
        }
        print(json.dumps(payload, sort_keys=True, indent=2))
        if args.mode == "require" and reason:
            return 1
        return 0
    except Exception as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
