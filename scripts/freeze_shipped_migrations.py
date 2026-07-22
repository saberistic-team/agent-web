#!/usr/bin/env python3
"""Freeze digests for migrations that have shipped to production.

After Render deploy, the dedicated CI job ``Freeze shipped migrations`` waits
for ``/health`` then calls ``maybe_commit_freeze`` so any migration still
missing from ``FROZEN_MIGRATION_DIGESTS`` is recorded with a meta commit
(``deploy: freeze …``). New migrations stay editable until the next healthy
production deploy.

That commit cannot land directly on a protected branch — the repo's
workflow-governance ruleset (``docs/WORKFLOW_GOVERNANCE.md``, issue #359)
intentionally has zero bypass actors, so a bot pushing straight to ``main``
is rejected with a 422 ("Changes must be made through a pull request").
``maybe_commit_freeze`` instead commits onto a dedicated ``deploy/freeze-*``
branch, opens a PR against ``main``, and enables GitHub's native auto-merge
so the PR merges itself the instant a human CODEOWNER approves — no further
action needed, and no permanent ruleset bypass is introduced (see issue
#362 for the incident this replaced). Re-running against the same missing
versions reuses the existing branch/PR instead of opening a duplicate.
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


def freeze_branch_name(versions: list[str]) -> str:
    """Deterministic branch name so repeated runs for the same missing
    versions reuse one branch/PR instead of opening a duplicate each deploy.
    """
    return f"deploy/freeze-{'-'.join(versions)}"


def freeze_pr_body(versions: list[str]) -> str:
    joined = ", ".join(versions)
    return (
        "### deploy_freeze\n"
        f"- versions: `{joined}`\n"
        "- Automated, digest-only change: records `FROZEN_MIGRATION_DIGESTS` "
        "entries for migrations that already shipped to production. No "
        "migration SQL, application code, or test changes.\n"
        "- Opened as a PR (not a direct push) because the workflow-governance "
        "ruleset requires every change to `main` go through review — see "
        "`docs/WORKFLOW_GOVERNANCE.md`. Auto-merge is enabled: approving this "
        "PR is sufficient, no separate merge click needed.\n"
    )


def maybe_commit_freeze(repo: str, branch: str, *, root: Path | None = None) -> dict[str, Any]:
    """Open (or reuse) a PR that freezes newly shipped digests.

    Returns a result dict with ``frozen`` (versions added) and, when a PR was
    opened or reused, ``pr_number``/``pr_url``. ``sha`` is the new commit sha
    on the freeze branch, or ``None`` when reusing an already-open PR.
    No-op when everything is already frozen.
    """
    base = root or repo_root()
    missing = missing_frozen_digests(base)
    if not missing:
        print("freeze_migrations: nothing to freeze")
        return {"frozen": [], "sha": None}

    versions = list(missing.keys())
    message = commit_message(versions)
    head_branch = freeze_branch_name(versions)

    from github_api import (
        create_branch,
        enable_auto_merge,
        find_open_pr_for_branch,
        open_pull_request,
        put_files,
    )

    existing_pr = find_open_pr_for_branch(repo, head_branch)
    if existing_pr is not None:
        print(
            f"freeze_migrations: reusing existing PR #{existing_pr['number']} "
            f"({head_branch})"
        )
        return {
            "frozen": versions,
            "sha": None,
            "message": message,
            "pr_number": existing_pr["number"],
            "pr_url": existing_pr["html_url"],
        }

    files = build_freeze_files_at(base)
    create_branch(repo, head_branch, base_branch=branch)
    sha = put_files(repo, head_branch, files, message)
    pr = open_pull_request(
        repo,
        head=head_branch,
        base=branch,
        title=message,
        body=freeze_pr_body(versions),
    )
    enable_auto_merge(repo, pr["node_id"])
    print(
        f"freeze_migrations: opened PR #{pr['number']} ({head_branch}) -> {sha}"
    )
    return {
        "frozen": versions,
        "sha": sha,
        "message": message,
        "pr_number": pr["number"],
        "pr_url": pr["html_url"],
    }


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
