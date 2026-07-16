#!/usr/bin/env python3
"""Validate workflow governance: ownership, discovery, and review policy."""

from __future__ import annotations

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
RULESET_JSON = Path(".github/rulesets/independent-workflow-review.json")
RULESET_NAME = "Require independent review for workflow governance"

WORKFLOW_SCRIPT_RE = re.compile(r"python\s+scripts/([\w]+\.py)")
IMPORT_FROM_RE = re.compile(r"^\s*from\s+([\w]+)\s+import", re.M)
IMPORT_RE = re.compile(r"^\s*import\s+([\w]+)(?:\s|$)", re.M)
DYNAMIC_SCRIPT_RE = re.compile(r"scripts/([\w]+\.py)")

BOT_LOGIN_RE = re.compile(r"(?:\[bot\]|bot$|-bot$)", re.I)


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


def normalize_pattern(pattern: str) -> str:
    return pattern.lstrip("/")


def matching_owners(path: str, rules: list[tuple[str, list[str]]]) -> list[str]:
    """Return owners from the last matching CODEOWNERS rule."""
    owners: list[str] = []
    for pattern, candidate_owners in rules:
        if _glob_regex(normalize_pattern(pattern)).match(path):
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


def manifest_patterns(payload: dict[str, Any]) -> list[str]:
    protected = payload.get("protected_paths")
    if not isinstance(protected, list):
        return []
    patterns: list[str] = []
    for entry in protected:
        if isinstance(entry, dict) and isinstance(entry.get("path"), str):
            patterns.append(entry["path"])
    return patterns


def path_is_protected(path: str, patterns: list[str]) -> bool:
    return any(_glob_regex(pattern).match(path) for pattern in patterns)


def discover_workflow_scripts(root: Path) -> set[str]:
    """Return scripts/*.py entrypoints referenced by GitHub Actions workflows."""
    scripts: set[str] = set()
    workflows = root / ".github" / "workflows"
    if not workflows.is_dir():
        return scripts
    for workflow in sorted(workflows.glob("*.y*ml")):
        text = workflow.read_text(encoding="utf-8")
        for match in WORKFLOW_SCRIPT_RE.finditer(text):
            scripts.add(f"scripts/{match.group(1)}")
    return scripts


def discover_local_script_imports(script_path: Path, root: Path) -> set[str]:
    """Return other scripts/*.py modules imported by ``script_path``."""
    if not script_path.is_file():
        return set()
    text = script_path.read_text(encoding="utf-8")
    modules: set[str] = set()
    for pattern in (IMPORT_FROM_RE, IMPORT_RE):
        for match in pattern.finditer(text):
            modules.add(match.group(1))
    for match in DYNAMIC_SCRIPT_RE.finditer(text):
        modules.add(match.group(1).removesuffix(".py"))
    scripts_dir = root / "scripts"
    discovered: set[str] = set()
    for module in sorted(modules):
        candidate = scripts_dir / f"{module}.py"
        if candidate.is_file():
            discovered.add(candidate.relative_to(root).as_posix())
    return discovered


def transitive_privileged_scripts(root: Path, seeds: set[str]) -> set[str]:
    """Fail-closed closure of workflow entrypoints and their scripts/ imports."""
    pending = sorted(seeds)
    seen: set[str] = set()
    while pending:
        rel = pending.pop()
        if rel in seen:
            continue
        seen.add(rel)
        for dep in sorted(discover_local_script_imports(root / rel, root)):
            if dep not in seen:
                pending.append(dep)
    return seen


def manifest_script_paths(root: Path, patterns: list[str]) -> set[str]:
    files = repository_files(root)
    return {path for path in files if path.startswith("scripts/") and path_is_protected(path, patterns)}


def human_codeowner_logins(rules: list[tuple[str, list[str]]]) -> set[str]:
    logins: set[str] = set()
    for _, owners in rules:
        for owner in owners:
            login = owner.lstrip("@")
            if login and "bot" not in owner.lower():
                logins.add(login)
    return logins


def is_automation_login(login: str) -> bool:
    if not login:
        return True
    lowered = login.lower()
    return bool(BOT_LOGIN_RE.search(lowered) or "github-actions" in lowered)


def evaluate_independent_review(
    *,
    author: str,
    reviews: list[dict[str, Any]],
    last_push_sha: str,
    changed_paths: list[str],
    manifest_path_patterns: list[str],
    rules: list[tuple[str, list[str]]],
) -> list[str]:
    """Return errors when a protected-path PR lacks independent human approval."""
    if not any(path_is_protected(path, manifest_path_patterns) for path in changed_paths):
        return []

    human_owners = human_codeowner_logins(rules)
    qualifying: list[str] = []
    for review in reviews:
        if (review.get("state") or "").upper() != "APPROVED":
            continue
        login = ((review.get("user") or {}).get("login") or "").strip()
        if not login or is_automation_login(login):
            continue
        if login == author:
            continue
        if login not in human_owners:
            continue
        commit_id = review.get("commit_id") or (review.get("commit") or {}).get("oid")
        if last_push_sha and commit_id and commit_id != last_push_sha:
            continue
        qualifying.append(login)

    if qualifying:
        return []
    return [
        "protected path change lacks independent human CODEOWNER approval "
        f"(author={author!r}, qualifying_humans={sorted(human_owners)})"
    ]


def expected_ruleset_parameters() -> dict[str, Any]:
    return {
        "enforcement": "active",
        "require_code_owner_review": True,
        "required_approving_review_count": 1,
        "require_last_push_approval": True,
        "dismiss_stale_reviews_on_push": True,
        "required_review_thread_resolution": True,
        "bypass_actors": [],
    }


def validate_ruleset_export(export: dict[str, Any]) -> list[str]:
    """Validate a ruleset export matches independent-review requirements."""
    errors: list[str] = []
    expected = expected_ruleset_parameters()
    enforcement = export.get("enforcement")
    if enforcement != expected["enforcement"]:
        errors.append(
            f"ruleset enforcement must be {expected['enforcement']!r}, got {enforcement!r}"
        )
    bypass = export.get("bypass_actors")
    if bypass not in (None, []):
        errors.append("ruleset must not define bypass actors")

    rules = export.get("rules") or []
    pull_request = next(
        (rule for rule in rules if rule.get("type") == "pull_request"),
        None,
    )
    if pull_request is None:
        errors.append("ruleset must include a pull_request rule")
        return errors

    params = pull_request.get("parameters") or {}
    for key in (
        "require_code_owner_review",
        "require_last_push_approval",
        "dismiss_stale_reviews_on_push",
        "required_review_thread_resolution",
    ):
        if params.get(key) is not expected[key]:
            errors.append(
                f"ruleset parameter {key} must be {expected[key]!r}, got {params.get(key)!r}"
            )
    count = params.get("required_approving_review_count")
    if count != expected["required_approving_review_count"]:
        errors.append(
            "ruleset parameter required_approving_review_count must be "
            f"{expected['required_approving_review_count']!r}, got {count!r}"
        )
    return errors


def _github_api(token: str, path: str) -> Any:
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
        return json.loads(response.read().decode("utf-8"))


def fetch_live_ruleset(repo: str, token: str) -> dict[str, Any] | None:
    owner, name = repo.split("/", 1)
    rulesets = _github_api(token, f"/repos/{owner}/{name}/rulesets")
    if not isinstance(rulesets, list):
        return None
    summary = next((item for item in rulesets if item.get("name") == RULESET_NAME), None)
    if summary is None:
        return None
    ruleset_id = summary.get("id")
    if ruleset_id is None:
        return None
    return _github_api(token, f"/repos/{owner}/{name}/rulesets/{ruleset_id}")


def validate_live_ruleset(root: Path = ROOT) -> list[str]:
    """Compare the live repository ruleset to the checked-in policy."""
    repo = (os.environ.get("GITHUB_REPOSITORY") or "").strip()
    token = (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()
    if not repo or not token:
        return []

    try:
        live = fetch_live_ruleset(repo, token)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
        return [f"unable to fetch live ruleset for {repo}: {exc}"]

    if live is None:
        return [f"live ruleset not found: {RULESET_NAME!r}"]

    errors = validate_ruleset_export(live)
    if errors:
        return [f"live ruleset drift: {error}" for error in errors]

    checked_in = root / RULESET_JSON
    if checked_in.is_file():
        checked = json.loads(checked_in.read_text(encoding="utf-8"))
        checked_errors = validate_ruleset_export(checked)
        if checked_errors:
            return [f"checked-in ruleset invalid: {error}" for error in checked_errors]
    return []


def validate(root: Path = ROOT, *, check_live_ruleset: bool | None = None) -> list[str]:
    """Return policy errors; an empty list means governance checks passed."""
    manifest_path = root / MANIFEST
    codeowners_path = root / CODEOWNERS
    if not manifest_path.is_file():
        return [f"missing manifest: {MANIFEST}"]
    if not codeowners_path.is_file():
        return [f"missing CODEOWNERS: {CODEOWNERS}"]

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    protected = payload.get("protected_paths")
    if not isinstance(protected, list) or not protected:
        return ["manifest must contain a non-empty protected_paths list"]

    patterns = manifest_patterns(payload)
    if not patterns:
        return ["manifest must contain at least one protected path pattern"]

    rules = parse_codeowners(codeowners_path.read_text(encoding="utf-8"))
    codeowners_patterns = {normalize_pattern(pattern) for pattern, _ in rules}
    manifest_pattern_set = {normalize_pattern(pattern) for pattern in patterns}

    errors: list[str] = []
    for pattern in sorted(manifest_pattern_set - codeowners_patterns):
        errors.append(f"manifest pattern missing from CODEOWNERS: {pattern}")
    for pattern in sorted(codeowners_patterns - manifest_pattern_set):
        errors.append(f"CODEOWNERS pattern missing from manifest: {pattern}")

    files = repository_files(root)
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
            bot_owners = [owner for owner in owners if "bot" in owner.lower()]
            if bot_owners:
                errors.append(
                    f"protected path has bot CODEOWNER: {path} ({', '.join(bot_owners)})"
                )

    workflow_scripts = discover_workflow_scripts(root)
    for script in sorted(workflow_scripts):
        if not path_is_protected(script, patterns):
            errors.append(f"workflow-invoked script is not governed: {script}")

    seeds = set(workflow_scripts) | manifest_script_paths(root, patterns)
    for script in sorted(transitive_privileged_scripts(root, seeds)):
        if not path_is_protected(script, patterns):
            errors.append(f"privileged transitive helper is not governed: {script}")

    ruleset_path = root / RULESET_JSON
    if ruleset_path.is_file():
        errors.extend(validate_ruleset_export(json.loads(ruleset_path.read_text(encoding="utf-8"))))

    if check_live_ruleset is None:
        check_live_ruleset = os.environ.get("VALIDATE_LIVE_RULESET", "1").strip().lower() not in {
            "0",
            "false",
            "no",
        }
    if check_live_ruleset:
        errors.extend(validate_live_ruleset(root))

    return errors


def main() -> int:
    errors = validate()
    if errors:
        for error in errors:
            print(f"FAIL: {error}", file=sys.stderr)
        return 1
    print(
        "PASS: workflow governance is complete "
        "(ownership, discovery, transitive helpers, ruleset policy)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
