#!/usr/bin/env python3
"""Validate workflow-governance coverage, discovery, and review policy."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = Path(".github/workflow-governance-paths.json")
CODEOWNERS = Path(".github/CODEOWNERS")
RULESET = Path(".github/rulesets/independent-workflow-review.json")
WORKFLOWS_DIR = Path(".github/workflows")
SCRIPTS_DIR = Path("scripts")

WORKFLOW_SCRIPT_RE = re.compile(r"python\s+scripts/([A-Za-z0-9_]+\.py)")
SCRIPT_IMPORT_RE = re.compile(
    r"(?:^|\n)\s*(?:from|import)\s+([A-Za-z_][A-Za-z0-9_]*)",
    re.MULTILINE,
)

# Privileged helpers that must stay protected even when not workflow-invoked.
EXPLICIT_PRIVILEGED_SCRIPTS = frozenset(
    {
        "scripts/copilot_agent.py",
    }
)

RULESET_NAME = "Require independent review for workflow governance"
REQUIRED_RULESET_PARAMETERS = {
    "dismiss_stale_reviews_on_push": True,
    "require_code_owner_review": True,
    "require_last_push_approval": True,
    "required_approving_review_count": 1,
    "required_review_thread_resolution": True,
}


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
    return pattern.lstrip("/")


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


def discover_workflow_scripts(root: Path) -> list[str]:
    """Return workflow-invoked ``scripts/*.py`` entrypoints."""
    scripts: set[str] = set()
    workflows = root / WORKFLOWS_DIR
    if not workflows.is_dir():
        return []
    for workflow in sorted(workflows.glob("*.yml")) + sorted(workflows.glob("*.yaml")):
        text = workflow.read_text()
        for match in WORKFLOW_SCRIPT_RE.findall(text):
            scripts.add(f"scripts/{match}")
    return sorted(scripts)


def script_imports(script_path: Path, scripts_dir: Path) -> set[str]:
    """Return local ``scripts/*.py`` modules imported by ``script_path``."""
    if not script_path.is_file():
        return set()
    text = script_path.read_text()
    imports: set[str] = set()
    for module in SCRIPT_IMPORT_RE.findall(text):
        candidate = scripts_dir / f"{module}.py"
        if candidate.is_file():
            imports.add(f"scripts/{module}.py")
    return imports


def transitive_script_closure(entrypoints: list[str], root: Path) -> list[str]:
    """Return workflow entrypoints and their privileged ``scripts/`` helpers."""
    scripts_dir = root / SCRIPTS_DIR
    seen: set[str] = set()
    queue = list(entrypoints)
    while queue:
        rel = queue.pop(0)
        if rel in seen:
            continue
        seen.add(rel)
        imports = script_imports(scripts_dir / Path(rel).name, scripts_dir)
        for imported in sorted(imports):
            if imported not in seen:
                queue.append(imported)
    return sorted(seen)


def privileged_script_inventory(root: Path) -> list[str]:
    """Compute the fail-closed privileged script inventory."""
    workflow_scripts = discover_workflow_scripts(root)
    closure = transitive_script_closure(workflow_scripts, root)
    inventory = sorted(set(closure) | EXPLICIT_PRIVILEGED_SCRIPTS)
    return inventory


def path_covered_by_manifest(path: str, patterns: list[str]) -> bool:
    return any(_glob_regex(pattern).match(path) for pattern in patterns)


def manifest_patterns(payload: dict[str, Any]) -> list[str]:
    protected = payload.get("protected_paths")
    if not isinstance(protected, list):
        return []
    patterns: list[str] = []
    for entry in protected:
        if isinstance(entry, dict) and isinstance(entry.get("path"), str):
            patterns.append(entry["path"])
    return patterns


def validate_manifest_codeowners_alignment(
    manifest_patterns_list: list[str], rules: list[tuple[str, list[str]]]
) -> list[str]:
    """Fail when manifest and CODEOWNERS drift in either direction."""
    manifest_set = {normalize_pattern(pattern) for pattern in manifest_patterns_list}
    codeowners_set = {normalize_pattern(pattern) for pattern, _ in rules}
    errors: list[str] = []
    for pattern in sorted(manifest_set - codeowners_set):
        errors.append(f"manifest pattern missing from CODEOWNERS: {pattern}")
    for pattern in sorted(codeowners_set - manifest_set):
        errors.append(f"CODEOWNERS pattern missing from manifest: {pattern}")
    return errors


def validate_privileged_script_coverage(
    root: Path, manifest_patterns_list: list[str]
) -> list[str]:
    """Fail when workflow execution or imports reach unprotected helpers."""
    errors: list[str] = []
    for script in privileged_script_inventory(root):
        if not path_covered_by_manifest(script, manifest_patterns_list):
            errors.append(
                "privileged script lacks governance coverage: "
                f"{script} (workflow entrypoint or transitive helper)"
            )
    return errors


def validate_ruleset_document(path: Path) -> list[str]:
    """Validate the checked-in ruleset matches required review mechanics."""
    if not path.is_file():
        return [f"missing ruleset document: {path.as_posix()}"]
    payload = json.loads(path.read_text())
    errors: list[str] = []
    if payload.get("name") != RULESET_NAME:
        errors.append(f"ruleset name must be {RULESET_NAME!r}")
    if payload.get("enforcement") != "active":
        errors.append("ruleset enforcement must be active")
    bypass = payload.get("bypass_actors")
    if bypass:
        errors.append("ruleset must not define bypass actors")
    rules = payload.get("rules")
    if not isinstance(rules, list) or not rules:
        errors.append("ruleset must define at least one rule")
        return errors
    pull_request_rules = [
        rule for rule in rules if isinstance(rule, dict) and rule.get("type") == "pull_request"
    ]
    if not pull_request_rules:
        errors.append("ruleset must include a pull_request rule")
        return errors
    parameters = pull_request_rules[0].get("parameters")
    if not isinstance(parameters, dict):
        errors.append("ruleset pull_request rule must include parameters")
        return errors
    for key, expected in REQUIRED_RULESET_PARAMETERS.items():
        if parameters.get(key) != expected:
            errors.append(
                f"ruleset parameter {key!r} must be {expected!r}, "
                f"got {parameters.get(key)!r}"
            )
    return errors


def normalize_login(login: str) -> str:
    return login.strip().lstrip("@")


def is_automation_login(login: str) -> bool:
    """True for GitHub Apps, bots, and other non-human actors."""
    normalized = normalize_login(login).lower()
    if normalized.endswith("[bot]"):
        return True
    if "bot" in normalized:
        return True
    if normalized in {"github-actions", "web-flow"}:
        return True
    return False


def human_codeowners_for_paths(
    paths: list[str], rules: list[tuple[str, list[str]]]
) -> set[str]:
    owners: set[str] = set()
    for path in paths:
        for owner in matching_owners(path, rules):
            if not is_automation_login(owner):
                owners.add(normalize_login(owner))
    return owners


def independent_codeowner_approval_satisfies(
    reviews: list[dict[str, Any]],
    *,
    pr_author: str,
    changed_paths: list[str],
    rules: list[tuple[str, list[str]]],
) -> tuple[bool, str]:
    """Return whether an independent human CODEOWNER approved the PR."""
    author = normalize_login(pr_author)
    eligible_owners = human_codeowners_for_paths(changed_paths, rules)
    if not eligible_owners:
        return False, "no human CODEOWNERS for changed protected paths"

    for review in reviews:
        if review.get("state") != "APPROVED":
            continue
        user = review.get("user") or {}
        login = normalize_login(str(user.get("login") or ""))
        if not login:
            continue
        if is_automation_login(login):
            continue
        if login == author:
            continue
        if login not in eligible_owners:
            continue
        return True, f"independent CODEOWNER approval from @{login}"

    bot_approvals = [
        normalize_login(str((review.get("user") or {}).get("login") or ""))
        for review in reviews
        if review.get("state") == "APPROVED"
        and is_automation_login(str((review.get("user") or {}).get("login") or ""))
    ]
    if bot_approvals:
        return False, "bot or automation approval cannot satisfy CODEOWNER review"
    author_approvals = [
        normalize_login(str((review.get("user") or {}).get("login") or ""))
        for review in reviews
        if review.get("state") == "APPROVED"
        and normalize_login(str((review.get("user") or {}).get("login") or "")) == author
    ]
    if author_approvals:
        return False, "author self-approval cannot satisfy CODEOWNER review"
    return False, "missing independent human CODEOWNER approval"


def validate(root: Path = ROOT) -> list[str]:
    """Return policy errors; an empty list means governance is complete."""
    manifest_path = root / MANIFEST
    codeowners_path = root / CODEOWNERS
    if not manifest_path.is_file():
        return [f"missing manifest: {MANIFEST}"]
    if not codeowners_path.is_file():
        return [f"missing CODEOWNERS: {CODEOWNERS}"]

    payload = json.loads(manifest_path.read_text())
    protected = payload.get("protected_paths")
    if not isinstance(protected, list) or not protected:
        return ["manifest must contain a non-empty protected_paths list"]

    patterns = manifest_patterns(payload)
    rules = parse_codeowners(codeowners_path.read_text())
    files = repository_files(root)
    errors: list[str] = []
    errors.extend(validate_manifest_codeowners_alignment(patterns, rules))
    errors.extend(validate_privileged_script_coverage(root, patterns))
    errors.extend(validate_ruleset_document(root / RULESET))

    for entry in protected:
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
            bot_owners = [owner for owner in owners if is_automation_login(owner)]
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
    print("PASS: workflow governance inventory, ownership, and ruleset are complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
