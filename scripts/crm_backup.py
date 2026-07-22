#!/usr/bin/env python3
"""Application-level CRM/analytics backup export and restore verification (#128).

Exports a redacted JSON manifest (table counts and non-sensitive aggregates only).
Never writes emails, session tokens, payment identifiers, or other PII to the
manifest. Operators store exports outside the repository.

Usage:
  python scripts/crm_backup.py export [--database-url URL] [--output PATH]
  python scripts/crm_backup.py verify [--database-url URL] [--snapshot PATH]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import psycopg
from psycopg.rows import dict_row

from app.analytics_event_schema import SCHEMA_VERSION as ANALYTICS_SCHEMA_VERSION
from app.migrations.definitions import MIGRATIONS
from app.migrations.runner import apply_migrations
from app.pipeline_stages import PIPELINE_STAGE_LABELS, PIPELINE_STAGE_ORDER

MANIFEST_VERSION = "1"

# Tables whose row counts are included in every export/verify pass.
CRM_BACKUP_TABLES: tuple[str, ...] = (
    "schema_migrations",
    "project_briefs",
    "companies",
    "contacts",
    "source_records",
    "activities",
    "admin_users",
    "admin_sessions",
    "admin_login_rate_limits",
    "admin_login_flows",
    "audit_events",
    "research_records",
    "pipeline_stage_history",
    "import_batches",
    "import_batch_rows",
    "analytics_events",
    "analytics_sessions",
    "analytics_event_rate_limits",
    "icp_scoring_versions",
    "icp_scoring_rules",
    "company_icp_score_snapshots",
    "qualification_tier_history",
    "qualification_working_lists",
    "qualification_working_list_items",
)

# Distribution queries: (key, sql) — aggregates only, no row-level PII.
DISTRIBUTION_QUERIES: tuple[tuple[str, str], ...] = (
    (
        "companies.pipeline_stage",
        """
        SELECT pipeline_stage AS bucket, COUNT(*)::bigint AS n
        FROM companies
        GROUP BY pipeline_stage
        ORDER BY bucket
        """,
    ),
    (
        "project_briefs.status",
        """
        SELECT status AS bucket, COUNT(*)::bigint AS n
        FROM project_briefs
        GROUP BY status
        ORDER BY bucket
        """,
    ),
    (
        "import_batches.status",
        """
        SELECT status AS bucket, COUNT(*)::bigint AS n
        FROM import_batches
        GROUP BY status
        ORDER BY bucket
        """,
    ),
    (
        "audit_events.action",
        """
        SELECT action AS bucket, COUNT(*)::bigint AS n
        FROM audit_events
        GROUP BY action
        ORDER BY action
        LIMIT 100
        """,
    ),
    (
        "analytics_events.event_name",
        """
        SELECT event_name AS bucket, COUNT(*)::bigint AS n
        FROM analytics_events
        GROUP BY event_name
        ORDER BY event_name
        LIMIT 100
        """,
    ),
)

REDACTION_POLICY = {
    "policy": "counts_and_aggregates_only",
    "excluded_row_data": [
        "contact_value",
        "email",
        "brief",
        "website",
        "password_hash",
        "session_token",
        "stripe_session_id",
        "stripe_payment_intent_id",
        "metadata",
        "actor",
        "correlation_id",
    ],
}


def expected_latest_schema_version() -> str:
    return MIGRATIONS[-1].version


def expected_latest_migration_name() -> str:
    return MIGRATIONS[-1].name


def _table_count(conn: psycopg.Connection, table: str) -> int:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(f"SELECT COUNT(*)::bigint AS n FROM {table}")
        row = cur.fetchone()
    assert row is not None
    return int(row["n"])


def _table_exists(conn: psycopg.Connection, table: str) -> bool:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT to_regclass(%s) AS reg", (f"public.{table}",))
        row = cur.fetchone()
    assert row is not None
    return row["reg"] is not None


def _fetch_distribution(conn: psycopg.Connection, sql: str) -> dict[str, int]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql)
        rows = cur.fetchall()
    result: dict[str, int] = {}
    for row in rows:
        bucket = row["bucket"]
        key = "(null)" if bucket is None else str(bucket)
        result[key] = int(row["n"])
    return result


def _applied_schema_version(conn: psycopg.Connection) -> str | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT version
            FROM schema_migrations
            ORDER BY version DESC
            LIMIT 1
            """
        )
        row = cur.fetchone()
    if row is None:
        return None
    return str(row["version"])


def build_configuration_block() -> dict[str, Any]:
    return {
        "pipeline_stages": list(PIPELINE_STAGE_ORDER),
        "pipeline_stage_labels": dict(PIPELINE_STAGE_LABELS),
        "analytics_schema_version": ANALYTICS_SCHEMA_VERSION,
        "expected_latest_schema_version": expected_latest_schema_version(),
        "expected_latest_migration_name": expected_latest_migration_name(),
    }


def build_snapshot(conn: psycopg.Connection) -> dict[str, Any]:
    """Build a redacted export manifest from a live database connection."""
    table_counts: dict[str, int] = {}
    for table in CRM_BACKUP_TABLES:
        if not _table_exists(conn, table):
            raise RuntimeError(f"missing required table: {table}")
        table_counts[table] = _table_count(conn, table)

    distributions: dict[str, dict[str, int]] = {}
    for key, sql in DISTRIBUTION_QUERIES:
        distributions[key] = _fetch_distribution(conn, sql)

    schema_version = _applied_schema_version(conn)
    return {
        "manifest_version": MANIFEST_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "schema_version": schema_version,
        "latest_migration_name": _latest_migration_name(conn),
        "table_counts": table_counts,
        "distributions": distributions,
        "configuration": build_configuration_block(),
        "redaction": REDACTION_POLICY,
    }


def _latest_migration_name(conn: psycopg.Connection) -> str | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT name
            FROM schema_migrations
            ORDER BY version DESC
            LIMIT 1
            """
        )
        row = cur.fetchone()
    if row is None:
        return None
    return str(row["name"])


def validate_snapshot_structure(snapshot: Any) -> list[str]:
    """Return human-readable errors when *snapshot* is not a valid manifest."""
    errors: list[str] = []
    if not isinstance(snapshot, dict):
        return ["snapshot must be a JSON object"]

    if snapshot.get("manifest_version") != MANIFEST_VERSION:
        errors.append(f"manifest_version must be {MANIFEST_VERSION!r}")

    for field in ("exported_at", "schema_version", "table_counts", "configuration"):
        if field not in snapshot:
            errors.append(f"missing required field: {field}")

    table_counts = snapshot.get("table_counts")
    if not isinstance(table_counts, dict):
        errors.append("table_counts must be an object")
    else:
        for table in CRM_BACKUP_TABLES:
            if table not in table_counts:
                errors.append(f"table_counts missing {table}")
            elif not isinstance(table_counts[table], int):
                errors.append(f"table_counts[{table}] must be an integer")

    configuration = snapshot.get("configuration")
    if not isinstance(configuration, dict):
        errors.append("configuration must be an object")
    elif configuration.get("expected_latest_schema_version") != expected_latest_schema_version():
        errors.append(
            "configuration.expected_latest_schema_version does not match "
            "current application migrations"
        )

    return errors


def compare_table_counts(
    actual: dict[str, int], expected: dict[str, int]
) -> list[str]:
    mismatches: list[str] = []
    for table in CRM_BACKUP_TABLES:
        if actual.get(table) != expected.get(table):
            mismatches.append(
                f"{table}: expected {expected.get(table)!r}, got {actual.get(table)!r}"
            )
    return mismatches


def verify_restore(
    conn: psycopg.Connection,
    *,
    expected_snapshot: dict[str, Any] | None = None,
    require_migration_noop: bool = True,
) -> dict[str, Any]:
    """Validate a restored database: tables, counts, schema version, migrations."""
    errors: list[str] = []
    warnings: list[str] = []

    for table in CRM_BACKUP_TABLES:
        if not _table_exists(conn, table):
            errors.append(f"missing table: {table}")

    table_counts: dict[str, int] = {}
    if not errors:
        for table in CRM_BACKUP_TABLES:
            table_counts[table] = _table_count(conn, table)

    schema_version = _applied_schema_version(conn)
    latest_expected = expected_latest_schema_version()
    if schema_version != latest_expected:
        errors.append(
            f"schema_version {schema_version!r} != expected latest {latest_expected!r}"
        )

    applied_versions: list[str] = []
    if require_migration_noop and not errors:
        applied_versions = apply_migrations(conn)
        if applied_versions:
            errors.append(
                "apply_migrations applied new versions on restore target: "
                + ", ".join(applied_versions)
            )

    count_mismatches: list[str] = []
    if expected_snapshot is not None:
        structure_errors = validate_snapshot_structure(expected_snapshot)
        errors.extend(structure_errors)
        if not structure_errors and table_counts:
            expected_counts = expected_snapshot.get("table_counts", {})
            if isinstance(expected_counts, dict):
                count_mismatches = compare_table_counts(
                    table_counts,
                    {k: int(v) for k, v in expected_counts.items()},
                )
                if count_mismatches:
                    errors.extend(count_mismatches)

    if expected_snapshot is None and schema_version and schema_version != latest_expected:
        warnings.append(
            "no snapshot provided; only structural checks and migration noop were run"
        )

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "schema_version": schema_version,
        "table_counts": table_counts,
        "migrations_applied": applied_versions,
        "count_mismatches": count_mismatches,
        "expected_latest_schema_version": latest_expected,
    }


def resolve_database_url(cli_value: str | None) -> str:
    url = (cli_value or os.environ.get("DATABASE_URL") or "").strip()
    if not url:
        raise SystemExit(
            "DATABASE_URL is required (pass --database-url or set the env var)"
        )
    return url


def cmd_export(database_url: str, output_path: str | None) -> int:
    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        snapshot = build_snapshot(conn)

    structure_errors = validate_snapshot_structure(snapshot)
    if structure_errors:
        for err in structure_errors:
            print(f"FAIL structure: {err}", file=sys.stderr)
        return 1

    payload = json.dumps(snapshot, indent=2, sort_keys=True) + "\n"
    if output_path:
        with open(output_path, "w", encoding="utf-8") as fh:
            fh.write(payload)
        print(f"PASS wrote redacted snapshot to {output_path}")
    else:
        sys.stdout.write(payload)
    return 0


def cmd_verify(
    database_url: str,
    snapshot_path: str | None,
    *,
    require_migration_noop: bool,
) -> int:
    expected: dict[str, Any] | None = None
    if snapshot_path:
        with open(snapshot_path, encoding="utf-8") as fh:
            expected = json.load(fh)

    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        result = verify_restore(
            conn,
            expected_snapshot=expected,
            require_migration_noop=require_migration_noop,
        )

    print(json.dumps(result, indent=2, sort_keys=True))
    if result["ok"]:
        print("PASS restore verification")
        return 0

    for err in result["errors"]:
        print(f"FAIL {err}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    export_parser = sub.add_parser("export", help="Write a redacted CRM snapshot")
    export_parser.add_argument("--database-url", default=None)
    export_parser.add_argument(
        "--output",
        default=None,
        help="Write JSON to this path (default: stdout)",
    )

    verify_parser = sub.add_parser("verify", help="Verify a restored database")
    verify_parser.add_argument("--database-url", default=None)
    verify_parser.add_argument(
        "--snapshot",
        default=None,
        help="Optional export manifest for table-count parity checks",
    )
    verify_parser.add_argument(
        "--allow-pending-migrations",
        action="store_true",
        help="Skip the migration-noop check (for pre-migration restore targets)",
    )

    args = parser.parse_args(argv)
    database_url = resolve_database_url(args.database_url)

    if args.command == "export":
        return cmd_export(database_url, args.output)
    if args.command == "verify":
        return cmd_verify(
            database_url,
            args.snapshot,
            require_migration_noop=not args.allow_pending_migrations,
        )
    raise SystemExit(f"unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
