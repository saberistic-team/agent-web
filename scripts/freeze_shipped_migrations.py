#!/usr/bin/env python3
"""Freeze digests for migrations that have shipped to production.

After Render deploy, the dedicated CI job ``Freeze shipped migrations`` waits
for ``/health`` then calls ``maybe_commit_freeze`` so any migration still
missing from ``FROZEN_MIGRATION_DIGESTS`` is recorded on ``main`` with a meta
commit (``deploy: freeze …``) that does not re-trigger Render. New migrations
stay editable until the next healthy production deploy.
"""

from __future__ import annotations

import argparse
import importlib.util
import re
import sys
from pathlib import Path
from typing import Any

COMMIT_PREFIX = "deploy: freeze migrations"

_DEFINITIONS = Path("app/migrations/definitions.py")
_FROZEN_DICT_RE = re.compile(
    r"(FROZEN_MIGRATION_DIGESTS: dict\[str, str\] = \{\n)(.*?)(\n\})",
    re.DOTALL,
)


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def load_definitions(root: Path | None = None) -> Any:
    """Load definitions.py without importing ``app.migrations`` (avoids psycopg)."""
    path = (root or repo_root()) / _DEFINITIONS
    spec = importlib.util.spec_from_file_location("migration_definitions_freeze", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    # dataclasses reads sys.modules[cls.__module__] while processing the class.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def missing_frozen_digests(root: Path | None = None) -> dict[str, str]:
    """Return version→digest for registry entries not yet in FROZEN_MIGRATION_DIGESTS."""
    mod = load_definitions(root)
    frozen: dict[str, str] = dict(mod.FROZEN_MIGRATION_DIGESTS)
    missing: dict[str, str] = {}
    for migration in mod.MIGRATIONS:
        if migration.version in frozen:
            continue
        missing[migration.version] = mod.migration_content_digest(migration)
    return missing


def format_frozen_block(digests: dict[str, str]) -> str:
    lines = [f'    "{version}": "{digest}",' for version, digest in digests.items()]
    return "\n".join(lines)


def merged_frozen_digests(root: Path | None = None) -> dict[str, str]:
    """Existing frozen digests plus any still-unfrozen registry versions."""
    mod = load_definitions(root)
    merged = dict(mod.FROZEN_MIGRATION_DIGESTS)
    for migration in mod.MIGRATIONS:
        if migration.version not in merged:
            merged[migration.version] = mod.migration_content_digest(migration)
    return {
        migration.version: merged[migration.version]
        for migration in mod.MIGRATIONS
        if migration.version in merged
    }


def apply_freeze_to_definitions(text: str, digests: dict[str, str]) -> str:
    """Rewrite the FROZEN_MIGRATION_DIGESTS literal in definitions.py source."""
    block = format_frozen_block(digests)
    match = _FROZEN_DICT_RE.search(text)
    if match is None:
        raise RuntimeError("FROZEN_MIGRATION_DIGESTS block not found in definitions.py")
    updated = _FROZEN_DICT_RE.sub(rf"\1{block}\3", text, count=1)
    updated = updated.replace(
        "# When adding a new migration, leave prior entries unchanged and freeze the\n"
        "# new version only after it has shipped to production.\n",
        "# When adding a new migration, leave prior entries unchanged. The CI job\n"
        '# "Freeze shipped migrations" (scripts/freeze_shipped_migrations.py) freezes\n'
        "# new versions after a healthy production deploy — do not hand-edit shipped digests.\n",
    )
    updated = updated.replace(
        "# When adding a new migration, leave prior entries unchanged. Post-deploy\n"
        "# (scripts/freeze_shipped_migrations.py) freezes new versions after a healthy\n"
        "# production deploy — do not hand-edit shipped digests.\n",
        "# When adding a new migration, leave prior entries unchanged. The CI job\n"
        '# "Freeze shipped migrations" (scripts/freeze_shipped_migrations.py) freezes\n'
        "# new versions after a healthy production deploy — do not hand-edit shipped digests.\n",
    )
    return updated


def build_freeze_files_at(root: Path) -> list[tuple[str, bytes]]:
    missing = missing_frozen_digests(root)
    if not missing:
        return []
    merged = merged_frozen_digests(root)
    path = root / _DEFINITIONS
    rel = _DEFINITIONS.as_posix()
    new_text = apply_freeze_to_definitions(path.read_text(encoding="utf-8"), merged)
    return [(rel, new_text.encode("utf-8"))]


def commit_message(versions: list[str]) -> str:
    joined = ", ".join(versions)
    return f"{COMMIT_PREFIX} ({joined})"


def maybe_commit_freeze(repo: str, branch: str, *, root: Path | None = None) -> dict[str, Any]:
    """Commit newly shipped digests to ``branch`` when any are missing.

    Returns a result dict with ``frozen`` (versions added) and optional ``sha``.
    No-op when everything is already frozen.
    """
    base = root or repo_root()
    missing = missing_frozen_digests(base)
    if not missing:
        print("freeze_migrations: nothing to freeze")
        return {"frozen": [], "sha": None}

    files = build_freeze_files_at(base)
    from github_api import put_files

    versions = list(missing.keys())
    message = commit_message(versions)
    sha = put_files(repo, branch, files, message)
    print(f"freeze_migrations: froze {', '.join(versions)} -> {sha}")
    return {"frozen": versions, "sha": sha, "message": message}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Print missing versions; exit 1 if any are unfrozen",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Rewrite local definitions.py (no git push)",
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Commit freeze to GitHub (requires --repo)",
    )
    parser.add_argument(
        "--wait-healthy",
        action="store_true",
        help="Poll production /health before --commit (uses DEPLOY_BASE_URL)",
    )
    parser.add_argument("--repo", default="", help="owner/name for --commit")
    parser.add_argument("--branch", default="main")
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Repo root (default: parent of scripts/)",
    )
    args = parser.parse_args(argv)
    root = (args.root or repo_root()).resolve()
    scripts_dir = Path(__file__).resolve().parent
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))

    missing = missing_frozen_digests(root)
    if args.check:
        if missing:
            print("missing:", ", ".join(missing))
            for version, digest in missing.items():
                print(f'  "{version}": "{digest}",')
            return 1
        print("ok: all migrations frozen")
        return 0

    if args.write:
        files = build_freeze_files_at(root)
        if not files:
            print("nothing to write")
            return 0
        for rel, content in files:
            (root / rel).write_bytes(content)
            print(f"wrote {rel}")
        return 0

    if args.commit:
        repo = (args.repo or "").strip()
        if not repo:
            print("FAIL: --repo required with --commit", file=sys.stderr)
            return 1
        if args.wait_healthy:
            from screenshot_deploy import resolve_base_url, wait_healthy

            base_url = resolve_base_url()
            print(f"freeze_migrations: waiting for health at {base_url}")
            health = wait_healthy(base_url)
            slim = {k: v for k, v in health.items() if not str(k).startswith("_")}
            print(f"freeze_migrations: healthy {slim}")
        maybe_commit_freeze(repo, args.branch, root=root)
        return 0

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
