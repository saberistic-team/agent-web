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
ROOT = Path(__file__).resolve().parent.parent


def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    return float(raw)


def _run(cmd: list[str]) -> int:
    print("+", " ".join(cmd), flush=True)
    proc = subprocess.run(cmd, cwd=ROOT)
    return int(proc.returncode)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
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
        ]
    )
    # Fresh coverage for the integration suite (do not merge with unit).
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
        ]
    )

    if unit_rc != 0:
        print(
            f"FAIL: unit coverage of app/ is below {args.unit_min:g}% "
            "(or unit tests failed).",
            file=sys.stderr,
        )
    if integration_rc != 0:
        print(
            f"FAIL: integration coverage of app/ is below {args.integration_min:g}% "
            "(or integration tests failed).",
            file=sys.stderr,
        )
    if unit_rc == 0 and integration_rc == 0:
        print(
            f"OK: unit>={args.unit_min:g}% and integration>={args.integration_min:g}% "
            "on app/"
        )
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
