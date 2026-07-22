#!/usr/bin/env python3
"""Validate CODEOWNERS coverage for security-sensitive workflow paths."""

from __future__ import annotations

import ast
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = Path(".github/workflow-governance-paths.json")
CODEOWNERS = Path(".github/CODEOWNERS")
WORKFLOWS_DIR = Path(".github/workflows")
RULESET_NAME = "Require independent review for workflow governance"
WORKFLOW_SCRIPT_RE = re.compile(r"python\s+scripts/([a-zA-Z0-9_]+)\.py")
SCRIPT_PATH_RE = re.compile(r"scripts/([a-z_]+)\.py")
HUMAN_CODEOWNERS = frozenset({"@saberistic"})
AUTOMATION_LOGIN_MARKERS = ("bot", "agent", "app", "copilot")


def is_repository_wide_pattern(pattern: str) -> bool:
    """Return whether a CODEOWNERS pattern assigns ownership to the entire repo."""
    return pattern.lstrip("/") == "*"


def _glob_regex(pattern: str) -> re.Pattern[str]:
    """Compile the CODEOWNERS subset used by this repository."""
    pattern = pattern.lstrip("/")
    if pattern == "*":
        return re.compile("^.*$")
    result = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "*" and pattern[index : index + 3] == "**/":
            result.append("(?:.*/)?")
            index += 3
        elif char == "*" and pattern[index : index + 2] == "**":
            result.append(".*")
            index += 2
        elif char == "*":
            result.append("[^/]*")
            index += 1
        elif char == "?":
            result.append("[^/]")
            index += 1
        else:
            result.append(re.escape(char))
            index += 1
    return re.compile("^" + "".join(result) + "$")


def parse_codeowners(contents: str) -> list[tuple[str, list[str]]]:
    """Return ordered CODEOWNERS patterns and their owners."""
    rules = []
    for number, line in enumerate(contents.splitlines(), start=1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        fields = line.split()
        if len(fields) < 2:
            raise ValueError(f"CODEOWNERS line {number} has no owner")
        rules.append((fields[0], fields[1:]))
    return rules


def matching_owners(path: str, rules: list[tuple[str, list[str]]]) -> list[str]:
    """Return owners from the last matching CODEOWNERS rule."""
    owners: list[str] = []
    for pattern, candidate_owners in rules:
        if _glob_regex(pattern).match(path):
            owners = candidate_owners
    return owners


def repository_files(root: Path) -> list[str]:
    return sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
        and ".git" not in path.relative_to(root).parts
        and "__pycache__" not in path.relative_to(root).parts
    )


def load_manifest(root: Path) -> tuple[list[str], list[str]]:
    """Return manifest patterns and any load errors."""
    manifest_path = root / MANIFEST
    if not manifest_path.is_file():
        return [], [f"missing manifest: {MANIFEST}"]

    payload = json.loads(manifest_path.read_text())
    protected = payload.get("protected_paths")
    if not isinstance(protected, list) or not protected:
        return [], ["manifest must contain a non-empty protected_paths list"]

    patterns: list[str] = []
    errors: list[str] = []
    for entry in protected:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            errors.append(f"invalid protected path entry: {entry!r}")
            continue
        patterns.append(entry["path"])
    return patterns, errors


def discover_workflow_entrypoints(root: Path) -> set[str]:
    """Return scripts/*.py paths referenced directly by workflow run steps."""
    scripts: set[str] = set()
    workflows = root / WORKFLOWS_DIR
    if not workflows.is_dir():
        return scripts
    for workflow in sorted(workflows.glob("*.yml")):
        for match in WORKFLOW_SCRIPT_RE.finditer(workflow.read_text()):
            scripts.add(f"scripts/{match.group(1)}.py")
    return scripts


def local_script_imports(script_path: Path) -> set[str]:
    """Return local scripts/*.py dependencies imported by one orchestration script."""
    text = script_path.read_text()
    modules: set[str] = set()
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".", 1)[0])
    for match in SCRIPT_PATH_RE.finditer(text):
        modules.add(match.group(1))

    scripts_dir = script_path.parent
    imports: set[str] = set()
    for module in sorted(modules):
        candidate = scripts_dir / f"{module}.py"
        if candidate.is_file():
            imports.add(f"scripts/{module}.py")
    return imports


def transitive_script_closure(entrypoints: set[str], root: Path) -> set[str]:
    """Follow local imports from workflow entrypoints and protected scripts."""
    seen: set[str] = set()
    queue = sorted(path for path in entrypoints if path.startswith("scripts/"))
    while queue:
        rel_path = queue.pop()
        if rel_path in seen:
            continue
        seen.add(rel_path)
        script_path = root / rel_path
        if not script_path.is_file():
            continue
        for imported in local_script_imports(script_path):
            if imported not in seen:
                queue.append(imported)
    return seen


def path_matches_any_pattern(path: str, patterns: list[str]) -> bool:
    return any(_glob_regex(pattern).match(path) for pattern in patterns)


def manifest_matched_files(files: list[str], patterns: list[str]) -> set[str]:
    matched: set[str] = set()
    for path in files:
        if path_matches_any_pattern(path, patterns):
            matched.add(path)
    return matched


def validate_manifest_ownership(
    root: Path,
    *,
    patterns: list[str],
    rules: list[tuple[str, list[str]]],
    files: list[str],
) -> list[str]:
    errors: list[str] = []
    for pattern in patterns:
        matching_files = [path for path in files if _glob_regex(pattern).match(path)]
        if not matching_files:
            errors.append(f"protected pattern matches no repository file: {pattern}")
            continue
        for path in matching_files:
            owners = matching_owners(path, rules)
            if not owners:
                errors.append(f"unowned protected path: {path} (from {pattern})")
                continue
            bot_owners = [owner for owner in owners if "bot" in owner.lower()]
            if bot_owners:
                errors.append(
                    f"protected path has bot CODEOWNER: {path} ({', '.join(bot_owners)})"
                )
    return errors


def validate_codeowners_manifest_sync(
    root: Path,
    *,
    patterns: list[str],
    rules: list[tuple[str, list[str]]],
    files: list[str],
) -> list[str]:
    """Fail when CODEOWNERS and manifest drift in either direction."""
    errors: list[str] = []
    governance_files = manifest_matched_files(files, patterns)

    for pattern, _owners in rules:
        if is_repository_wide_pattern(pattern):
            continue
        normalized = pattern.lstrip("/")
        for path in files:
            if not _glob_regex(normalized).match(path):
                continue
            if path in governance_files:
                continue
            errors.append(
                "CODEOWNERS pattern covers a path outside the governance manifest: "
                f"{pattern} -> {path}"
            )

    discovered = discover_workflow_entrypoints(root)
    protected_scripts = {
        path for path in governance_files if path.startswith("scripts/") and path.endswith(".py")
    }
    discovered.update(transitive_script_closure(discovered | protected_scripts, root))
    for script in sorted(discovered):
        if script not in files:
            errors.append(f"workflow references missing script: {script}")
            continue
        if not path_matches_any_pattern(script, patterns):
            errors.append(
                "workflow-invoked or transitive orchestration script is not protected: "
                f"{script}"
            )
    return errors


@dataclass(frozen=True)
class PullRequestReview:
    author_login: str
    state: str
    commit_oid: str


def is_automation_identity(login: str) -> bool:
    lowered = login.lower()
    return any(marker in lowered for marker in AUTOMATION_LOGIN_MARKERS) or lowered.endswith(
        "[bot]"
    )


def independent_codeowner_approval_satisfied(
    *,
    pr_author: str,
    head_sha: str,
    reviews: list[PullRequestReview],
    codeowners: frozenset[str] = HUMAN_CODEOWNERS,
) -> tuple[bool, str]:
    """Return whether an independent human CODEOWNER approved the current head."""
    for review in reviews:
        if review.state != "APPROVED":
            continue
        if review.commit_oid != head_sha:
            return False, "stale approval does not match current head commit"
        if is_automation_identity(review.author_login):
            return False, f"automation review cannot authorize merge: @{review.author_login}"
        owner = f"@{review.author_login}"
        if owner not in codeowners:
            return False, f"reviewer is not a human CODEOWNER: @{review.author_login}"
        if review.author_login == pr_author:
            return False, "author self-approval does not satisfy independent review"
        return True, f"independent CODEOWNER approval from @{review.author_login}"

    return False, "no independent human CODEOWNER approval for current head"


def validate_ruleset_payload(payload: dict[str, Any]) -> list[str]:
    """Validate the live/exported ruleset enforces independent human review."""
    errors: list[str] = []
    if payload.get("enforcement") != "active":
        errors.append("ruleset enforcement must be active")
    if payload.get("bypass_actors"):
        errors.append("ruleset must not define bypass actors")

    rules = payload.get("rules")
    if not isinstance(rules, list) or not rules:
        return errors + ["ruleset must define pull_request rules"]

    pull_request_rule = next(
        (rule for rule in rules if isinstance(rule, dict) and rule.get("type") == "pull_request"),
        None,
    )
    if pull_request_rule is None:
        return errors + ["ruleset must include a pull_request rule"]

    parameters = pull_request_rule.get("parameters")
    if not isinstance(parameters, dict):
        return errors + ["pull_request rule must include parameters"]

    required_flags = {
        "require_code_owner_review": True,
        "require_last_push_approval": True,
        "dismiss_stale_reviews_on_push": True,
        "required_review_thread_resolution": True,
    }
    for flag, expected in required_flags.items():
        if parameters.get(flag) is not expected:
            errors.append(f"ruleset parameter {flag} must be {expected!r}")

    if parameters.get("required_approving_review_count", 0) < 1:
        errors.append("ruleset must require at least one approving review")
    return errors


def fetch_live_ruleset(
    *,
    repository: str,
    token: str,
    ruleset_name: str = RULESET_NAME,
) -> dict[str, Any]:
    """Fetch the active repository ruleset payload from GitHub."""
    owner, repo = repository.split("/", 1)
    request = urllib.request.Request(
        f"https://api.github.com/repos/{owner}/{repo}/rulesets",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        summaries = json.load(response)

    match = next(
        (
            item
            for item in summaries
            if isinstance(item, dict) and item.get("name") == ruleset_name
        ),
        None,
    )
    if match is None:
        raise RuntimeError(f"ruleset not found: {ruleset_name}")

    ruleset_id = match["id"]
    detail_request = urllib.request.Request(
        f"https://api.github.com/repos/{owner}/{repo}/rulesets/{ruleset_id}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(detail_request, timeout=30) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise RuntimeError("ruleset detail payload must be an object")
    return payload


def validate_live_ruleset(root: Path = ROOT) -> list[str]:
    """Validate the live repository ruleset when token and repo env are present."""
    if os.environ.get("VERIFY_LIVE_GOVERNANCE_RULESET", "").strip() not in {"1", "true", "yes"}:
        return []

    repository = os.environ.get("GITHUB_REPOSITORY", "").strip()
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not repository or not token:
        return ["live ruleset verification requested but GITHUB_REPOSITORY or GITHUB_TOKEN is missing"]

    try:
        payload = fetch_live_ruleset(repository=repository, token=token)
    except (RuntimeError, urllib.error.URLError, json.JSONDecodeError) as exc:
        return [f"unable to fetch live ruleset: {exc}"]

    return [f"live ruleset: {error}" for error in validate_ruleset_payload(payload)]


# A live-ruleset failure is repository-**settings** drift (GitHub, not this
# checked-in policy) — no Builder codegen on any product PR can fix it, yet
# it fails the same "test" job every product PR runs. Before this alert
# existed, that drift produced only a bare CI red X on `main`, indistinguishable
# among dozens of Builder/Reviewer bot pushes; it recurred silently for hours
# (see docs/WORKFLOW_GOVERNANCE.md "Ruleset drift alerting") because nothing
# paged a human. File (or refresh) one tracking issue instead.
GOVERNANCE_DRIFT_ISSUE_TITLE = "Live ruleset drift: workflow-governance enforcement is not active"


def ruleset_drift_errors(errors: list[str]) -> list[str]:
    """Return the subset of ``errors`` caused by live-ruleset drift, not code."""
    return [
        error
        for error in errors
        if error.startswith("live ruleset:") or error.startswith("unable to fetch live ruleset:")
    ]


def _find_open_issue_by_title(repo: str, title: str) -> int | None:
    from github_api import api, split_repo

    owner, name = split_repo(repo)
    page = 1
    while page <= 5:
        batch = api(
            "GET",
            f"/repos/{owner}/{name}/issues?state=open&per_page=50&page={page}",
        )
        if not batch:
            return None
        for item in batch:
            if item.get("title") == title and "pull_request" not in item:
                return int(item["number"])
        if len(batch) < 50:
            return None
        page += 1
    return None


def report_ruleset_drift(repo: str, errors: list[str]) -> None:
    """File (or refresh) a plain tracking issue when the live ruleset drifted.

    Opt-in via ``REPORT_GOVERNANCE_DRIFT=1`` (set only for pushes to the
    default branch in ci.yml) so PR runs and local/test invocations never
    open issues. Deliberately omits every ``status:*`` / ``type:*`` /
    ``priority:*`` / ``agent:*`` orchestration label — this is a repository
    **settings** problem the Planner/Builder/dispatcher pipeline must not try
    to "fix" with a code PR (see docs/WORKFLOW_GOVERNANCE.md "Recovery and
    break-glass": only a human repository admin can restore ruleset
    enforcement, and never via an automation token).
    """
    drift = ruleset_drift_errors(errors)
    if not drift:
        return
    if os.environ.get("REPORT_GOVERNANCE_DRIFT", "").strip() not in {"1", "true", "yes"}:
        return
    if not repo or not os.environ.get("GITHUB_TOKEN", "").strip():
        return

    from github_api import GitHubError, api, post_issue_comment, split_repo

    run_url = ""
    server_url = os.environ.get("GITHUB_SERVER_URL", "").strip()
    run_id = os.environ.get("GITHUB_RUN_ID", "").strip()
    if server_url and run_id:
        run_url = f"{server_url}/{repo}/actions/runs/{run_id}"

    body_lines = [
        "### governance_ruleset_drift",
        *(f"- error: `{error}`" for error in drift),
        f"- detected_in_run: {run_url or '(local run, no CI run URL)'}",
        "",
        "The **live** GitHub ruleset has drifted from the checked-in "
        "`.github/rulesets/independent-workflow-review.json` policy. This is "
        "a repository-settings problem — no code change on any Builder PR "
        "can fix it, and every PR that rebases onto this `main` will fail "
        "the same CI check until it is repaired.",
        "",
        "@saberistic: please follow "
        "`docs/WORKFLOW_GOVERNANCE.md` 'Recovery and break-glass' to restore "
        "enforcement, then re-run "
        "`VERIFY_LIVE_GOVERNANCE_RULESET=1 python scripts/validate_workflow_governance.py` "
        "to confirm before closing this issue.",
    ]
    body = "\n".join(body_lines)

    try:
        existing = _find_open_issue_by_title(repo, GOVERNANCE_DRIFT_ISSUE_TITLE)
        if existing is None:
            owner, name = split_repo(repo)
            api(
                "POST",
                f"/repos/{owner}/{name}/issues",
                body={"title": GOVERNANCE_DRIFT_ISSUE_TITLE, "body": body},
            )
        else:
            post_issue_comment(repo, existing, body)
    except GitHubError as exc:
        # Alerting is best-effort: never let a notification failure mask the
        # underlying drift as a hard crash of the validator itself.
        print(f"warning: failed to report governance ruleset drift: {exc}", file=sys.stderr)


def validate(root: Path = ROOT) -> list[str]:
    """Return policy errors; an empty list means ownership is complete."""
    codeowners_path = root / CODEOWNERS
    if not codeowners_path.is_file():
        return [f"missing CODEOWNERS: {CODEOWNERS}"]

    patterns, errors = load_manifest(root)
    if errors:
        return errors

    rules = parse_codeowners(codeowners_path.read_text())
    files = repository_files(root)
    errors.extend(validate_manifest_ownership(root, patterns=patterns, rules=rules, files=files))
    errors.extend(
        validate_codeowners_manifest_sync(
            root, patterns=patterns, rules=rules, files=files
        )
    )
    errors.extend(validate_live_ruleset(root))
    return errors


def main() -> int:
    errors = validate()
    if errors:
        for error in errors:
            print(f"FAIL: {error}", file=sys.stderr)
        repository = os.environ.get("GITHUB_REPOSITORY", "").strip()
        report_ruleset_drift(repository, errors)
        return 1
    print(
        "PASS: workflow-governance paths are human-owned, manifest-synchronized, "
        "and workflow-discovered scripts are protected"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
