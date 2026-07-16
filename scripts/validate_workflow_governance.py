#!/usr/bin/env python3
"""Validate workflow governance: ownership, discovery, and ruleset alignment."""

from __future__ import annotations

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
RULESET = Path(".github/rulesets/independent-workflow-review.json")
WORKFLOWS = Path(".github/workflows")
RULESET_NAME = "Require independent review for workflow governance"

WORKFLOW_SCRIPT_RE = re.compile(
    r"(?:^|[\s\"'])(?:python3?\s+)?scripts/([\w][\w_]*\.py)",
    re.MULTILINE,
)

# Agent App slugs and generic GitHub bot markers — never independent humans.
BOT_LOGIN_MARKERS = (
    "bot",
    "github-actions",
    "copilot",
    "dependabot",
    "cursor",
)
KNOWN_AGENT_BOTS = frozenset(
    {
        "saberistic-agent-web-planner",
        "saberistic-agent-web-builder",
        "saberistic-agent-web-reviewer",
        "saberistic-agent-web-docs",
    }
)


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


def load_manifest(root: Path) -> dict[str, Any] | None:
    manifest_path = root / MANIFEST
    if not manifest_path.is_file():
        return None
    payload = json.loads(manifest_path.read_text())
    if not isinstance(payload, dict):
        return None
    return payload


def protected_patterns(payload: dict[str, Any]) -> list[str]:
    protected = payload.get("protected_paths")
    if not isinstance(protected, list):
        return []
    patterns: list[str] = []
    for entry in protected:
        if isinstance(entry, dict) and isinstance(entry.get("path"), str):
            patterns.append(entry["path"])
    return patterns


def workflow_entrypoint_exemptions(payload: dict[str, Any]) -> dict[str, str]:
    raw = payload.get("workflow_entrypoint_exemptions")
    if not isinstance(raw, list):
        return {}
    exemptions: dict[str, str] = {}
    for entry in raw:
        if isinstance(entry, dict) and isinstance(entry.get("path"), str):
            reason = entry.get("reason")
            exemptions[entry["path"]] = (
                reason if isinstance(reason, str) else "exempt workflow entrypoint"
            )
    return exemptions


def dynamic_script_imports(payload: dict[str, Any]) -> dict[str, list[str]]:
    raw = payload.get("dynamic_script_imports")
    if not isinstance(raw, dict):
        return {}
    result: dict[str, list[str]] = {}
    for key, value in raw.items():
        if isinstance(key, str) and isinstance(value, list):
            result[key] = [item for item in value if isinstance(item, str)]
    return result


def path_is_covered(path: str, patterns: list[str]) -> bool:
    return any(_glob_regex(pattern).match(path) for pattern in patterns)


def discover_workflow_script_entrypoints(root: Path) -> set[str]:
    """Return repo-relative script paths invoked from GitHub Actions workflows."""
    entrypoints: set[str] = set()
    workflows_dir = root / WORKFLOWS
    if not workflows_dir.is_dir():
        return entrypoints
    for workflow_path in sorted(workflows_dir.glob("*")):
        if workflow_path.suffix not in {".yml", ".yaml"}:
            continue
        text = workflow_path.read_text()
        for match in WORKFLOW_SCRIPT_RE.finditer(text):
            entrypoints.add(f"scripts/{match.group(1)}")
    return entrypoints


def _local_module_to_script(module: str) -> str:
    return f"scripts/{module}.py"


def discover_script_import_graph(root: Path) -> dict[str, set[str]]:
    """Map scripts/foo.py to other scripts/*.py it imports."""
    graph: dict[str, set[str]] = {}
    scripts_dir = root / "scripts"
    if not scripts_dir.is_dir():
        return graph
    for script_path in sorted(scripts_dir.glob("*.py")):
        rel = script_path.relative_to(root).as_posix()
        imports: set[str] = set()
        try:
            tree = ast.parse(script_path.read_text())
        except SyntaxError:
            graph[rel] = imports
            continue
        for node in ast.walk(tree):
            module_name: str | None = None
            if isinstance(node, ast.Import):
                for alias in node.names:
                    module_name = alias.name.split(".", 1)[0]
                    candidate = _local_module_to_script(module_name)
                    if (root / candidate).is_file():
                        imports.add(candidate)
            elif isinstance(node, ast.ImportFrom):
                if node.level != 0 or not node.module:
                    continue
                module_name = node.module.split(".", 1)[0]
                candidate = _local_module_to_script(module_name)
                if (root / candidate).is_file():
                    imports.add(candidate)
        graph[rel] = imports
    return graph


def transitive_script_closure(
    roots: set[str], graph: dict[str, set[str]]
) -> set[str]:
    """Return all scripts reachable from roots via local imports."""
    seen: set[str] = set()
    queue = sorted(roots)
    while queue:
        current = queue.pop()
        if current in seen:
            continue
        seen.add(current)
        for imported in sorted(graph.get(current, set())):
            if imported not in seen:
                queue.append(imported)
    return seen


def is_bot_login(login: str) -> bool:
    normalized = login.lstrip("@").lower()
    if normalized in {bot.lower() for bot in KNOWN_AGENT_BOTS}:
        return True
    return any(marker in normalized for marker in BOT_LOGIN_MARKERS)


def normalize_login(login: str) -> str:
    return login.lstrip("@").lower()


def independent_approval_error(
    reviewer_login: str,
    pr_author: str,
    codeowner_logins: list[str],
) -> str | None:
    """Return an error string when approval is not from an independent human CODEOWNER."""
    reviewer = normalize_login(reviewer_login)
    author = normalize_login(pr_author)
    if is_bot_login(reviewer):
        return f"non-human approval rejected: {reviewer_login}"
    if reviewer == author:
        return f"author self-approval rejected: {reviewer_login}"
    owners = {normalize_login(owner) for owner in codeowner_logins}
    if reviewer not in owners:
        return f"reviewer is not a CODEOWNER: {reviewer_login}"
    return None


def expected_ruleset_parameters() -> dict[str, Any]:
    ruleset_path = ROOT / RULESET
    payload = json.loads(ruleset_path.read_text())
    rules = payload.get("rules")
    if not isinstance(rules, list) or not rules:
        raise ValueError(f"{RULESET} must contain pull_request rules")
    for rule in rules:
        if isinstance(rule, dict) and rule.get("type") == "pull_request":
            parameters = rule.get("parameters")
            if isinstance(parameters, dict):
                return parameters
    raise ValueError(f"{RULESET} missing pull_request parameters")


def validate_ruleset_payload(payload: dict[str, Any]) -> list[str]:
    """Compare a live or fixture ruleset payload to repository policy."""
    errors: list[str] = []
    if payload.get("name") != RULESET_NAME:
        errors.append(f"unexpected ruleset name: {payload.get('name')!r}")
    if payload.get("enforcement") != "active":
        errors.append(
            f"ruleset enforcement must be active (got {payload.get('enforcement')!r})"
        )
    bypass = payload.get("bypass_actors")
    if bypass not in (None, []):
        errors.append(f"ruleset bypass_actors must be empty (got {bypass!r})")

    expected = expected_ruleset_parameters()
    rules = payload.get("rules")
    if not isinstance(rules, list):
        return errors + ["ruleset rules must be a list"]
    parameters: dict[str, Any] | None = None
    for rule in rules:
        if isinstance(rule, dict) and rule.get("type") == "pull_request":
            candidate = rule.get("parameters")
            if isinstance(candidate, dict):
                parameters = candidate
                break
    if parameters is None:
        return errors + ["ruleset missing pull_request parameters"]

    for key, expected_value in expected.items():
        live_value = parameters.get(key)
        if live_value != expected_value:
            errors.append(
                f"ruleset parameter {key} expected {expected_value!r}, got {live_value!r}"
            )
    return errors


def fetch_live_ruleset(repo: str, token: str) -> dict[str, Any]:
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/rulesets",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            rulesets = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"rulesets API HTTP {exc.code}: {exc.reason}") from exc
    if not isinstance(rulesets, list):
        raise RuntimeError("rulesets API returned unexpected payload")
    for summary in rulesets:
        if isinstance(summary, dict) and summary.get("name") == RULESET_NAME:
            ruleset_id = summary.get("id")
            if not isinstance(ruleset_id, int):
                raise RuntimeError("ruleset summary missing id")
            detail_request = urllib.request.Request(
                f"https://api.github.com/repos/{repo}/rulesets/{ruleset_id}",
                headers=request.headers,
            )
            with urllib.request.urlopen(detail_request, timeout=30) as response:
                payload = json.loads(response.read().decode())
            if isinstance(payload, dict):
                return payload
            raise RuntimeError("ruleset detail returned unexpected payload")
    raise RuntimeError(f"live ruleset not found: {RULESET_NAME}")


def validate_manifest_codeowners_sync(
    patterns: list[str], rules: list[tuple[str, list[str]]]
) -> list[str]:
    """Fail when manifest patterns and CODEOWNERS patterns diverge."""
    manifest_set = {normalize_pattern(pattern) for pattern in patterns}
    codeowners_set = {normalize_pattern(pattern) for pattern, _owners in rules}
    errors: list[str] = []
    for pattern in sorted(manifest_set - codeowners_set):
        errors.append(f"manifest pattern missing from CODEOWNERS: {pattern}")
    for pattern in sorted(codeowners_set - manifest_set):
        errors.append(f"CODEOWNERS pattern missing from manifest: {pattern}")
    return errors


def validate_ownership(
    root: Path,
    payload: dict[str, Any],
    rules: list[tuple[str, list[str]]],
    files: list[str],
) -> list[str]:
    protected = payload.get("protected_paths")
    if not isinstance(protected, list) or not protected:
        return ["manifest must contain a non-empty protected_paths list"]

    errors: list[str] = []
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
            bot_owners = [owner for owner in owners if is_bot_login(owner)]
            if bot_owners:
                errors.append(
                    f"protected path has bot CODEOWNER: {path} ({', '.join(bot_owners)})"
                )
    return errors


def validate_workflow_entrypoint_coverage(
    root: Path,
    payload: dict[str, Any],
    patterns: list[str],
) -> list[str]:
    entrypoints = discover_workflow_script_entrypoints(root)
    exemptions = workflow_entrypoint_exemptions(payload)
    errors: list[str] = []
    for entrypoint in sorted(entrypoints):
        if entrypoint in exemptions:
            continue
        if not (root / entrypoint).is_file():
            errors.append(f"workflow invokes missing script: {entrypoint}")
            continue
        if not path_is_covered(entrypoint, patterns):
            errors.append(
                f"workflow entrypoint lacks governance coverage: {entrypoint}"
            )
    return errors


def validate_transitive_coverage(
    root: Path,
    payload: dict[str, Any],
    patterns: list[str],
) -> list[str]:
    entrypoints = discover_workflow_script_entrypoints(root)
    exemptions = set(workflow_entrypoint_exemptions(payload))
    dynamic = dynamic_script_imports(payload)
    graph = discover_script_import_graph(root)

    roots = {path for path in entrypoints if path not in exemptions}
    for source, targets in dynamic.items():
        if source in roots or source in entrypoints:
            graph.setdefault(source, set()).update(targets)
            roots.add(source)

    closure = transitive_script_closure(roots, graph)
    errors: list[str] = []
    for script in sorted(closure):
        if script in exemptions:
            continue
        if not path_is_covered(script, patterns):
            errors.append(
                f"privileged transitive helper lacks governance coverage: {script}"
            )
    return errors


def validate_live_ruleset_if_requested(root: Path) -> list[str]:
    if os.environ.get("SKIP_LIVE_RULESET") == "1":
        return []
    if os.environ.get("GITHUB_ACTIONS") != "true" and os.environ.get(
        "VALIDATE_LIVE_RULESET"
    ) not in {"1", "true", "yes"}:
        return []

    repo = os.environ.get("GITHUB_REPOSITORY")
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not repo or not token:
        return [
            "live ruleset validation requested but GITHUB_REPOSITORY or token is missing"
        ]
    try:
        payload = fetch_live_ruleset(repo, token)
    except RuntimeError as exc:
        return [f"live ruleset fetch failed: {exc}"]
    return validate_ruleset_payload(payload)


def validate(root: Path = ROOT, *, check_live_ruleset: bool = False) -> list[str]:
    """Return policy errors; an empty list means governance checks passed."""
    manifest_path = root / MANIFEST
    codeowners_path = root / CODEOWNERS
    if not manifest_path.is_file():
        return [f"missing manifest: {MANIFEST}"]
    if not codeowners_path.is_file():
        return [f"missing CODEOWNERS: {CODEOWNERS}"]

    payload = load_manifest(root)
    if payload is None:
        return [f"invalid manifest: {MANIFEST}"]

    try:
        rules = parse_codeowners(codeowners_path.read_text())
    except ValueError as exc:
        return [str(exc)]

    patterns = protected_patterns(payload)
    if not patterns:
        return ["manifest must contain a non-empty protected_paths list"]

    files = repository_files(root)
    errors: list[str] = []
    errors.extend(validate_manifest_codeowners_sync(patterns, rules))
    errors.extend(validate_ownership(root, payload, rules, files))
    errors.extend(validate_workflow_entrypoint_coverage(root, payload, patterns))
    errors.extend(validate_transitive_coverage(root, payload, patterns))
    if check_live_ruleset:
        errors.extend(validate_live_ruleset_if_requested(root))
    return errors


def main() -> int:
    check_live = os.environ.get("GITHUB_ACTIONS") == "true"
    errors = validate(check_live_ruleset=check_live)
    if errors:
        for error in errors:
            print(f"FAIL: {error}", file=sys.stderr)
        return 1
    print("PASS: workflow governance boundary is complete and human-owned")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
