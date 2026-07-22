#!/usr/bin/env python3
"""Issue dependency guards (learned from #204 premature merge).

Machine-readable dependencies are required before Planner queues work and
before Dispatcher / Builder / Docs / Reviewer / Gate proceed:

- GitHub ``blockedBy`` relationships (preferred)
- Explicit body lines: ``Depends on: #199, #200`` / ``Blocked by: #199``
- Issue refs (``#N`` or full issue URLs) inside a ``## Dependencies`` /
  ``## Dependency`` section

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

from github_api import GitHubError, api, graphql, split_repo

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
    if parse_dependency_issue_numbers(
        f"## Dependencies\n{section}\n"
    ):
        return False
    # Non-empty prose without refs — fail closed (#204).
    return len(section) >= 12


def fetch_github_blocked_by(repo: str, issue: int) -> list[dict[str, Any]]:
    """Return GitHub ``blockedBy`` nodes for ``issue`` (may be empty)."""
    owner, name = split_repo(repo)
    data = graphql(
        """
        query($owner: String!, $name: String!, $number: Int!) {
          repository(owner: $owner, name: $name) {
            issue(number: $number) {
              blockedBy(first: 20) {
                nodes { number title state }
              }
            }
          }
        }
        """,
        {"owner": owner, "name": name, "number": int(issue)},
    )
    issue_data = ((data or {}).get("repository") or {}).get("issue") or {}
    nodes = ((issue_data.get("blockedBy") or {}).get("nodes")) or []
    return [node for node in nodes if node and node.get("number") is not None]


def _issue_state(repo: str, number: int) -> dict[str, Any]:
    owner, name = split_repo(repo)
    data = api("GET", f"/repos/{owner}/{name}/issues/{int(number)}") or {}
    return {
        "number": int(number),
        "title": data.get("title") or "",
        "state": (data.get("state") or "open").lower(),
    }


def open_dependency_blockers(
    repo: str,
    issue: int,
    *,
    body: str | None = None,
) -> list[dict[str, Any]]:
    """Return still-open blockers for ``issue``.

    Combines GitHub ``blockedBy`` with body ``Depends on:`` / Dependencies
    section refs. Closed dependencies are ignored.
    """
    if body is None:
        owner, name = split_repo(repo)
        issue_data = api("GET", f"/repos/{owner}/{name}/issues/{int(issue)}") or {}
        body = issue_data.get("body") or ""

    blockers: dict[int, dict[str, Any]] = {}

    try:
        for node in fetch_github_blocked_by(repo, issue):
            number = int(node["number"])
            state = str(node.get("state") or "OPEN").lower()
            if state != "open":
                continue
            blockers[number] = {
                "number": number,
                "title": node.get("title") or "",
                "state": "open",
                "source": "blocked_by",
            }
    except GitHubError:
        # GraphQL may be unavailable for some tokens; body refs still apply.
        pass

    for number in parse_dependency_issue_numbers(body or ""):
        if number == int(issue):
            continue
        if number in blockers:
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
) -> str | None:
    """Human-readable reason when dependencies are unmet, else ``None``."""
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

    blockers = open_dependency_blockers(repo, issue, body=body)
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
) -> None:
    """Raise ``GitHubError`` when open or unstructured dependencies remain."""
    reason = dependency_block_reason(repo, issue, body=body)
    if reason:
        raise GitHubError(reason)


def dispatcher_skip_comment(issue: int, reason: str) -> str:
    return (
        "### dispatcher_skip\n"
        f"- issue: `#{issue}`\n"
        "- reason: `open_dependencies`\n"
        f"- detail: {reason}\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--issue", type=int, required=True)
    parser.add_argument(
        "--mode",
        choices=("check", "require"),
        default="check",
        help="check prints JSON; require exits non-zero when blocked",
    )
    args = parser.parse_args(argv)

    try:
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
