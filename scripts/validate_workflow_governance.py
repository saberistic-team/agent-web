#!/usr/bin/env python3
"""Validate CODEOWNERS coverage for security-sensitive workflow paths."""

from __future__ import annotations

import ast
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = Path(".github/workflow-governance-paths.json")
CODEOWNERS = Path(".github/CODEOWNERS")
WORKFLOWS = Path(".github/workflows")
RULESET = Path(".github/rulesets/independent-workflow-review.json")
SCRIPT_RUN_RE = re.compile(r"python\s+scripts/([A-Za-z0-9_./-]+\.py)")

# Dynamic loads that static import tracing cannot see.
KNOWN_DYNAMIC_LOADS: dict[str, list[str]] = {
    "scripts/run_agent.py": ["scripts/screenshot_deploy.py"],
}

# GitHub App and automation logins that never satisfy independent human review.
NON_HUMAN_REVIEWER_PREFIXES = (
    "saberistic-agent-web-",
    "github-actions",
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


def codeowners_patterns(contents: str) -> list[str]:
    """Return normalized CODEOWNERS path patterns."""
    return [pattern.lstrip("/") for pattern, _owners in parse_codeowners(contents)]


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


def load_manifest(root: Path) -> tuple[list[str], list[dict[str, Any]]]:
    manifest_path = root / MANIFEST
    if not manifest_path.is_file():
        raise FileNotFoundError(f"missing manifest: {MANIFEST}")
    payload = json.loads(manifest_path.read_text())
    protected = payload.get("protected_paths")
    if not isinstance(protected, list) or not protected:
        raise ValueError("manifest must contain a non-empty protected_paths list")
    patterns: list[str] = []
    for entry in protected:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise ValueError(f"invalid protected path entry: {entry!r}")
        patterns.append(entry["path"])
    return patterns, protected


def path_matches_any(path: str, patterns: list[str]) -> bool:
    return any(_glob_regex(pattern).match(path) for pattern in patterns)


def discover_workflow_entrypoints(root: Path) -> set[str]:
    """Return scripts/*.py paths referenced by workflow run steps."""
    entrypoints: set[str] = set()
    workflows_dir = root / WORKFLOWS
    if not workflows_dir.is_dir():
        return entrypoints
    for workflow in sorted(workflows_dir.glob("*.yml")):
        text = workflow.read_text()
        for match in SCRIPT_RUN_RE.finditer(text):
            entrypoints.add(f"scripts/{match.group(1)}")
    return entrypoints


def _local_script_module_names(script_path: Path) -> set[str]:
    """Parse top-level imports that resolve to scripts/*.py in this repository."""
    try:
        tree = ast.parse(script_path.read_text())
    except SyntaxError:
        return set()
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".", 1)[0])
    return modules


def discover_transitive_script_helpers(root: Path, seeds: set[str]) -> set[str]:
    """Follow local script imports from workflow entrypoints (fail-closed closure)."""
    scripts_dir = root / "scripts"
    if not scripts_dir.is_dir():
        return set(seeds)

    available = {
        path.stem: f"scripts/{path.name}"
        for path in scripts_dir.glob("*.py")
        if path.is_file()
    }
    closure = set(seeds)
    queue = sorted(seeds)
    while queue:
        rel_path = queue.pop()
        script_file = root / rel_path
        if not script_file.is_file():
            continue
        imported = _local_script_module_names(script_file)
        for rel in KNOWN_DYNAMIC_LOADS.get(rel_path, []):
            closure.add(rel)
            queue.append(rel)
        for module in imported:
            target = available.get(module)
            if not target or target in closure:
                continue
            closure.add(target)
            queue.append(target)
    return closure


def discover_privileged_scripts(root: Path) -> set[str]:
    """Privileged boundary = workflow entrypoints plus transitive script helpers."""
    entrypoints = discover_workflow_entrypoints(root)
    return discover_transitive_script_helpers(root, entrypoints)


def normalize_login(login: str) -> str:
    return login.lstrip("@").lower()


def human_codeowners(rules: list[tuple[str, list[str]]]) -> set[str]:
    owners: set[str] = set()
    for _pattern, candidate_owners in rules:
        for owner in candidate_owners:
            if "bot" not in owner.lower():
                owners.add(normalize_login(owner))
    return owners


def is_non_human_reviewer(login: str) -> bool:
    normalized = login.lower().strip()
    if not normalized:
        return True
    if normalized.endswith("[bot]"):
        return True
    if "bot" in normalized:
        return True
    return any(normalized.startswith(prefix) for prefix in NON_HUMAN_REVIEWER_PREFIXES)


@dataclass(frozen=True)
class PullRequestReview:
    login: str
    state: str
    submitted_at: str
    stale: bool = False


def protected_changed_files(
    changed_files: list[str], protected_patterns: list[str]
) -> list[str]:
    return [path for path in changed_files if path_matches_any(path, protected_patterns)]


def evaluate_merge_authorization(
    *,
    pr_author: str,
    changed_files: list[str],
    reviews: list[PullRequestReview],
    protected_patterns: list[str],
    last_push_at: str,
    rules: list[tuple[str, list[str]]] | None = None,
) -> list[str]:
    """Return errors when a protected-path PR lacks independent human CODEOWNER approval."""
    protected = protected_changed_files(changed_files, protected_patterns)
    if not protected:
        return []

    codeowner_humans = human_codeowners(rules or [])
    errors: list[str] = []
    qualifying: list[str] = []

    for review in reviews:
        if review.state != "APPROVED":
            continue
        if review.stale:
            errors.append(
                f"stale approval from {review.login} does not satisfy "
                "require_last_push_approval"
            )
            continue
        if review.submitted_at < last_push_at:
            errors.append(
                f"approval from {review.login} predates the last push and is not valid"
            )
            continue
        if is_non_human_reviewer(review.login):
            errors.append(
                f"non-human approval from {review.login} cannot satisfy CODEOWNER review"
            )
            continue
        if normalize_login(review.login) == normalize_login(pr_author):
            errors.append(
                f"author self-approval from {review.login} cannot satisfy independent review"
            )
            continue

        owns_protected = False
        reviewer = normalize_login(review.login)
        for path in protected:
            owners = matching_owners(path, rules or [])
            if any(reviewer == normalize_login(owner) for owner in owners):
                owns_protected = True
                break
        if not owns_protected:
            errors.append(
                f"approval from {review.login} is not from a CODEOWNER of the "
                "changed protected paths"
            )
            continue
        qualifying.append(review.login)

    if not qualifying:
        errors.append(
            "protected-path change requires one independent human CODEOWNER approval"
        )
    return errors


def validate_ruleset_export(ruleset: dict[str, Any]) -> list[str]:
    """Validate the checked-in ruleset matches required independent-review settings."""
    errors: list[str] = []
    if ruleset.get("name") != RULESET_NAME:
        errors.append(f"ruleset name must be {RULESET_NAME!r}")
    if ruleset.get("enforcement") != "active":
        errors.append("ruleset enforcement must be active")
    bypass = ruleset.get("bypass_actors")
    if bypass:
        errors.append("ruleset must not define bypass actors")
    rules = ruleset.get("rules")
    if not isinstance(rules, list) or not rules:
        return errors + ["ruleset must define at least one rule"]
    pull_request_rules = [
        rule for rule in rules if isinstance(rule, dict) and rule.get("type") == "pull_request"
    ]
    if len(pull_request_rules) != 1:
        errors.append("ruleset must define exactly one pull_request rule")
        return errors
    params = pull_request_rules[0].get("parameters")
    if not isinstance(params, dict):
        return errors + ["pull_request rule must include parameters"]
    expected = {
        "dismiss_stale_reviews_on_push": True,
        "require_code_owner_review": True,
        "require_last_push_approval": True,
        "required_approving_review_count": 1,
        "required_review_thread_resolution": True,
    }
    for key, value in expected.items():
        if params.get(key) != value:
            errors.append(f"ruleset parameter {key} must be {value!r}")
    return errors


def validate_manifest_codeowners_sync(
    manifest_patterns: list[str], codeowners_text: str
) -> list[str]:
    """Fail when manifest patterns and CODEOWNERS governance patterns drift."""
    codeowners = codeowners_patterns(codeowners_text)
    errors: list[str] = []
    manifest_set = set(manifest_patterns)
    codeowners_set = set(codeowners)
    for pattern in sorted(manifest_set - codeowners_set):
        errors.append(
            f"manifest pattern missing from CODEOWNERS: {pattern}"
        )
    for pattern in sorted(codeowners_set - manifest_set):
        errors.append(
            f"CODEOWNERS pattern missing from manifest: {pattern}"
        )
    return errors


def validate_privileged_script_coverage(
    root: Path, manifest_patterns: list[str]
) -> list[str]:
    """Fail closed when workflow execution reaches scripts outside the manifest."""
    privileged = discover_privileged_scripts(root)
    errors: list[str] = []
    for script in sorted(privileged):
        if not path_matches_any(script, manifest_patterns):
            errors.append(
                f"privileged script missing from manifest: {script} "
                "(add to workflow-governance-paths.json and CODEOWNERS)"
            )
    return errors


def validate(root: Path = ROOT) -> list[str]:
    """Return policy errors; an empty list means ownership is complete."""
    codeowners_path = root / CODEOWNERS
    if not codeowners_path.is_file():
        return [f"missing CODEOWNERS: {CODEOWNERS}"]

    try:
        manifest_patterns, _protected = load_manifest(root)
    except (FileNotFoundError, ValueError) as exc:
        return [str(exc)]

    rules = parse_codeowners(codeowners_path.read_text())
    files = repository_files(root)
    errors: list[str] = []

    errors.extend(validate_manifest_codeowners_sync(
        manifest_patterns, codeowners_path.read_text()
    ))
    errors.extend(validate_privileged_script_coverage(root, manifest_patterns))

    ruleset_path = root / RULESET
    if ruleset_path.is_file():
        errors.extend(validate_ruleset_export(json.loads(ruleset_path.read_text())))

    for pattern in manifest_patterns:
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


def main() -> int:
    errors = validate()
    if errors:
        for error in errors:
            print(f"FAIL: {error}", file=sys.stderr)
        return 1
    print(
        "PASS: privileged workflow boundary is enumerated, synchronized, "
        "and human-owned"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
