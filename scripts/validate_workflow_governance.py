#!/usr/bin/env python3
"""Validate CODEOWNERS coverage for security-sensitive workflow paths."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = Path(".github/workflow-governance-paths.json")
CODEOWNERS = Path(".github/CODEOWNERS")


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


def validate(root: Path = ROOT) -> list[str]:
    """Return policy errors; an empty list means ownership is complete."""
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

    rules = parse_codeowners(codeowners_path.read_text())
    files = repository_files(root)
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
    print("PASS: every protected workflow-governance path has human CODEOWNERS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
