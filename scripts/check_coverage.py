#!/usr/bin/env python3
"""Enforce service coverage gates for Reviewer / CI.

Unit tests must cover >= 90% of `app/`.
Integration tests must cover >= 70% of `app/`.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

DEFAULT_UNIT_MIN = 90.0
DEFAULT_INTEGRATION_MIN = 70.0


def _root() -> Path:
    override = (os.environ.get("COVERAGE_ROOT") or "").strip()
    if override:
        return Path(override).resolve()
    return Path(__file__).resolve().parent.parent


def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    return float(raw)


def _run(cmd: list[str], *, cwd: Path) -> int:
    print("+", " ".join(cmd), f"(cwd={cwd})", flush=True)
    proc = subprocess.run(cmd, cwd=cwd)
    return int(proc.returncode)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Repo root to measure (default: COVERAGE_ROOT or this repo)",
    )
    parser.add_argument(
        "--unit-min",
        type=float,
        default=_env_float("COVERAGE_UNIT_MIN", DEFAULT_UNIT_MIN),
    )
    parser.add_argument(
        "--integration-min",
        type=float,
        default=_env_float("COVERAGE_INTEGRATION_MIN", DEFAULT_INTEGRATION_MIN),
    )
    args = parser.parse_args(argv)
    root = (args.root or _root()).resolve()
    if not (root / "app").is_dir():
        print(f"FAIL: no app/ under {root}", file=sys.stderr)
        return 1

    py = sys.executable
    unit_rc = _run(
        [
            py,
            "-m",
            "pytest",
            "-q",
            "-m",
            "unit",
            "--cov=app",
            "--cov-branch",
            "--cov-report=term-missing",
            f"--cov-fail-under={args.unit_min:g}",
        ],
        cwd=root,
    )
    integration_rc = _run(
        [
            py,
            "-m",
            "pytest",
            "-q",
            "-m",
            "integration",
            "--cov=app",
            "--cov-branch",
            "--cov-report=term-missing",
            f"--cov-fail-under={args.integration_min:g}",
        ],
        cwd=root,
    )

    if unit_rc != 0:
        print(
            f"FAIL: unit coverage of app/ is below {args.unit_min:g}% "
            f"(or unit tests failed) under {root}.",
            file=sys.stderr,
        )
    if integration_rc != 0:
        print(
            f"FAIL: integration coverage of app/ is below {args.integration_min:g}% "
            f"(or integration tests failed) under {root}.",
            file=sys.stderr,
        )
    if unit_rc == 0 and integration_rc == 0:
        print(
            f"OK: unit>={args.unit_min:g}% and integration>={args.integration_min:g}% "
            f"on app/ ({root})"
        )
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
