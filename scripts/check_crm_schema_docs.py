#!/usr/bin/env python3
"""Fail CI when docs/CRM_SCHEMA.md drifts from migrations 001–016 (#277)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.crm_schema_doc_contract import CRM_SCHEMA_DOC, validate_crm_schema_doc  # noqa: E402


def main() -> int:
    errors = validate_crm_schema_doc(CRM_SCHEMA_DOC)
    if not errors:
        print(f"OK: {CRM_SCHEMA_DOC} matches the canonical CRM schema contract.")
        return 0
    print(f"CRM schema documentation drift ({len(errors)} issue(s)):", file=sys.stderr)
    for error in errors:
        print(f"  - {error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
