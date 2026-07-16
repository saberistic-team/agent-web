#!/usr/bin/env python3
"""Validate CODEOWNERS coverage for security-sensitive workflow paths."""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = Path(".github/workflow-governance-paths.json")
CODEOWNERS = Path(".github/CODEOWNERS")
RULESET_SPEC = Path(".github/rulesets/independent-workflow-review.json")
WORKFLOWS_DIR = Path(".github/workflows")
SCRIPTS_DIR = Path("scripts")

WORKFLOW_SCRIPT_RE = re.compile(
    r"\bpython(?:3)?\s+scripts/([A-Za-z0-9_]+\.py)\b"
)
IMPORTLIB_SCRIPT_RE = re.compile(
    r"""['"]scripts/([A-Za-z0-9_]+\.py)['"]|"""
    r"""parent\s*/\s*['"]([A-Za-z0-9_]+\.py)['"]"""
)

RULESET_NAME = "Require independent review for workflow governance"


def _glob_regex(pattern: str) -> re.Pattern[str]:
    """Compile the CODEOWNERS subset used by this repository."""
    pattern = pattern.lstrip("/")
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


def normalize_pattern(pattern: str) -> str:
    """Normalize manifest/CODEOWNERS patterns for comparison."""
    value = pattern.strip()
    if not value.startswith("/"):
        value = f"/{value}"
    return value


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


def codeowner_patterns(rules: list[tuple[str, list[str]]]) -> list[str]:
    return [pattern for pattern, _owners in rules]


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


def load_manifest(root: Path) -> tuple[dict[str, Any] | None, list[str]]:
    manifest_path = root / MANIFEST
    if not manifest_path.is_file():
        return None, [f"missing manifest: {MANIFEST}"]
    payload = json.loads(manifest_path.read_text())
    protected = payload.get("protected_paths")
    if not isinstance(protected, list) or not protected:
        return payload, ["manifest must contain a non-empty protected_paths list"]
    return payload, []


def manifest_patterns(payload: dict[str, Any]) -> list[str]:
    protected = payload.get("protected_paths") or []
    patterns: list[str] = []
    for entry in protected:
        if isinstance(entry, dict) and isinstance(entry.get("path"), str):
            patterns.append(entry["path"])
    return patterns


def path_matches_any(path: str, patterns: list[str]) -> bool:
    return any(_glob_regex(pattern).match(path) for pattern in patterns)


def discover_workflow_scripts(root: Path) -> set[str]:
    """Return repo-relative script paths referenced by workflow run commands."""
    discovered: set[str] = set()
    workflows = root / WORKFLOWS_DIR
    if not workflows.is_dir():
        return discovered
    for workflow in sorted(workflows.glob("*.yml")) + sorted(workflows.glob("*.yaml")):
        text = workflow.read_text()
        for match in WORKFLOW_SCRIPT_RE.findall(text):
            discovered.add(f"scripts/{match}")
    return discovered


def _script_module_name(script_path: str) -> str:
    return Path(script_path).stem


def _imports_from_script(path: Path) -> set[str]:
    """Return sibling script module names imported by ``path``."""
    try:
        tree = ast.parse(path.read_text())
    except SyntaxError:
        return set()
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".", 1)[0])
    for match in IMPORTLIB_SCRIPT_RE.findall(path.read_text()):
        for group in match:
            if group:
                modules.add(Path(group).stem)
    return modules


def discover_transitive_script_helpers(
    root: Path, seeds: set[str]
) -> set[str]:
    """Return privileged scripts reachable via imports from ``seeds``."""
    scripts_root = root / SCRIPTS_DIR
    if not scripts_root.is_dir():
        return set()

    known = {
        path.relative_to(root).as_posix()
        for path in scripts_root.glob("*.py")
        if path.is_file()
    }
    module_to_path = {_script_module_name(path): path for path in known}

    closure = {path for path in seeds if path in known}
    frontier = list(closure)
    while frontier:
        current = frontier.pop()
        script_path = root / current
        if not script_path.is_file():
            continue
        for module in _imports_from_script(script_path):
            imported = module_to_path.get(module)
            if imported and imported not in closure:
                closure.add(imported)
                frontier.append(imported)
    return closure


def discover_required_privileged_scripts(root: Path, patterns: list[str]) -> set[str]:
    """Fail-closed inventory: workflows + transitive imports from protected seeds."""
    workflow_scripts = discover_workflow_scripts(root)
    manifest_scripts = {
        path
        for path in repository_files(root)
        if path.startswith("scripts/") and path_matches_any(path, patterns)
    }
    seeds = workflow_scripts | manifest_scripts
    return discover_transitive_script_helpers(root, seeds)


def validate_manifest_codeowners_sync(
    patterns: list[str], rules: list[tuple[str, list[str]]]
) -> list[str]:
    """Ensure manifest patterns and CODEOWNERS governance entries stay aligned."""
    manifest_set = {normalize_pattern(pattern) for pattern in patterns}
    codeowners_set = {normalize_pattern(pattern) for pattern in codeowner_patterns(rules)}
    errors: list[str] = []
    for pattern in sorted(manifest_set - codeowners_set):
        errors.append(f"manifest pattern missing from CODEOWNERS: {pattern}")
    for pattern in sorted(codeowners_set - manifest_set):
        errors.append(f"CODEOWNERS pattern missing from manifest: {pattern}")
    return errors


def validate_discovered_script_coverage(
    root: Path, patterns: list[str]
) -> list[str]:
    """Ensure every workflow entrypoint and transitive helper is manifest-protected."""
    required = discover_required_privileged_scripts(root, patterns)
    errors: list[str] = []
    for script in sorted(required):
        if not path_matches_any(script, patterns):
            errors.append(
                f"privileged script lacks governance coverage: {script} "
                "(add to workflow-governance-paths.json and CODEOWNERS)"
            )
    return errors


def is_automation_login(login: str) -> bool:
    lowered = login.lower()
    return lowered.endswith("[bot]") or lowered.endswith("-bot") or "bot" in lowered


def human_codeowner_logins(rules: list[tuple[str, list[str]]]) -> set[str]:
    humans: set[str] = set()
    for _pattern, owners in rules:
        for owner in owners:
            login = owner.lstrip("@")
            if not is_automation_login(login):
                humans.add(login)
    return humans


def evaluate_independent_codeowner_review(
    *,
    pr_author: str,
    reviews: list[dict[str, Any]],
    codeowner_logins: set[str],
    head_sha: str,
    dismiss_stale_reviews_on_push: bool = True,
) -> list[str]:
    """Return errors when a protected-path PR lacks independent human CODEOWNER approval."""
    submitted = [
        review
        for review in reviews
        if (review.get("state") or "").upper() in {"APPROVED", "CHANGES_REQUESTED"}
        and review.get("submitted_at")
    ]
    if not submitted:
        return ["no submitted pull request review"]

    qualifying: list[dict[str, Any]] = []
    for review in submitted:
        if (review.get("state") or "").upper() != "APPROVED":
            continue
        user = review.get("user") or {}
        login = str(user.get("login") or "")
        if not login:
            continue
        if is_automation_login(login):
            continue
        if login not in codeowner_logins:
            continue
        if login == pr_author:
            continue
        commit_id = review.get("commit_id")
        if dismiss_stale_reviews_on_push and commit_id and commit_id != head_sha:
            continue
        qualifying.append(review)

    if qualifying:
        return []
    return [
        "protected-path PR lacks independent human CODEOWNER approval "
        "(bot, author, stale, or non-owner reviews do not count)"
    ]


def expected_ruleset_parameters() -> dict[str, Any]:
    return {
        "dismiss_stale_reviews_on_push": True,
        "require_code_owner_review": True,
        "require_last_push_approval": True,
        "required_approving_review_count": 1,
        "required_review_thread_resolution": True,
    }


def validate_ruleset_document(ruleset: dict[str, Any]) -> list[str]:
    """Validate checked-in or live ruleset JSON against governance requirements."""
    errors: list[str] = []
    if ruleset.get("name") != RULESET_NAME:
        errors.append(f"ruleset name must be {RULESET_NAME!r}")
    if ruleset.get("target") != "branch":
        errors.append("ruleset target must be 'branch'")
    if ruleset.get("enforcement") != "active":
        errors.append("ruleset enforcement must be 'active'")
    bypass = ruleset.get("bypass_actors")
    if bypass not in (None, []):
        errors.append("ruleset bypass_actors must be empty")

    rules = ruleset.get("rules") or []
    pr_rules = [rule for rule in rules if rule.get("type") == "pull_request"]
    if len(pr_rules) != 1:
        errors.append("ruleset must contain exactly one pull_request rule")
        return errors

    params = pr_rules[0].get("parameters") or {}
    for key, expected in expected_ruleset_parameters().items():
        if params.get(key) != expected:
            errors.append(f"ruleset parameter {key!r} must be {expected!r}")
    return errors


def _github_api_get(path: str, token: str) -> Any:
    request = urllib.request.Request(
        f"https://api.github.com{path}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "agent-web-governance-validator",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode())


def fetch_live_ruleset(repo: str, token: str) -> dict[str, Any] | None:
    owner, name = repo.split("/", 1)
    payload = _github_api_get(f"/repos/{owner}/{name}/rulesets", token)
    for ruleset in payload:
        if ruleset.get("name") == RULESET_NAME:
            ruleset_id = ruleset.get("id")
            if ruleset_id is None:
                return None
            return _github_api_get(
                f"/repos/{owner}/{name}/rulesets/{ruleset_id}", token
            )
    return None


def validate_live_ruleset(repo: str, token: str) -> list[str]:
    """Compare live repository ruleset settings with governance requirements."""
    try:
        live = fetch_live_ruleset(repo, token)
    except urllib.error.HTTPError as exc:
        return [f"live ruleset lookup failed: HTTP {exc.code}"]
    except Exception as exc:  # pragma: no cover - network guard
        return [f"live ruleset lookup failed: {exc}"]

    if live is None:
        return [f"live ruleset not found: {RULESET_NAME!r}"]
    return validate_ruleset_document(live)


def validate(root: Path = ROOT) -> list[str]:
    """Return policy errors; an empty list means ownership is complete."""
    codeowners_path = root / CODEOWNERS
    if not codeowners_path.is_file():
        return [f"missing CODEOWNERS: {CODEOWNERS}"]

    payload, manifest_errors = load_manifest(root)
    if payload is None:
        return manifest_errors
    errors = list(manifest_errors)

    patterns = manifest_patterns(payload)
    rules = parse_codeowners(codeowners_path.read_text())
    files = repository_files(root)

    errors.extend(validate_manifest_codeowners_sync(patterns, rules))
    errors.extend(validate_discovered_script_coverage(root, patterns))

    for entry in payload.get("protected_paths") or []:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            errors.append(f"invalid protected path entry: {entry!r}")
            continue
        pattern = entry["path"]
        matching_files = [path for path in files if _glob_regex(pattern).match(path)]
        if not matching_files:
            errors.append(f"protected pattern matches no repository file: {pattern}")
            continue
        for path in matching_files:
            owners = matching_owners(path, rules)
            if not owners:
                errors.append(f"unowned protected path: {path} (from {pattern})")
                continue
            bot_owners = [owner for owner in owners if is_automation_login(owner.lstrip("@"))]
            if bot_owners:
                errors.append(
                    f"protected path has bot CODEOWNER: {path} ({', '.join(bot_owners)})"
                )

    ruleset_path = root / RULESET_SPEC
    if ruleset_path.is_file():
        errors.extend(validate_ruleset_document(json.loads(ruleset_path.read_text())))

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check-live-ruleset",
        action="store_true",
        help="Also validate the live GitHub ruleset via API (requires GITHUB_TOKEN)",
    )
    parser.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY", "saberistic-team/agent-web"),
        help="owner/name for live ruleset lookup",
    )
    args = parser.parse_args(argv)

    errors = validate()
    if args.check_live_ruleset:
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if not token:
            errors.append("missing GITHUB_TOKEN for live ruleset validation")
        else:
            errors.extend(validate_live_ruleset(args.repo, token))

    if errors:
        for error in errors:
            print(f"FAIL: {error}", file=sys.stderr)
        return 1
    print("PASS: workflow governance boundary is complete and human-owned")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
