"""Real-Postgres tests for legacy migration 013 reconciliation (#210)."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from app.actor_context import ActorContext
from app.crm_service import CrmService
from app.migrations.definitions import (
    FROZEN_MIGRATION_DIGESTS,
    MIGRATIONS,
    Migration,
    migration_content_digest,
)
from app.migrations.runner import apply_migrations
from app.repositories.postgres import PostgresPipelineRepository

# Earlier incompatible form of migration 013 that some deployed DBs recorded.
LEGACY_013_SQL = """
ALTER TABLE companies ADD COLUMN IF NOT EXISTS pipeline_stage TEXT;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS next_action TEXT;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS next_action_due_at TIMESTAMPTZ;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS owner TEXT;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS expected_value NUMERIC(12, 2);
ALTER TABLE companies ADD COLUMN IF NOT EXISTS stage_reason TEXT;

UPDATE companies SET pipeline_stage = 'researching' WHERE pipeline_stage IS NULL;
ALTER TABLE companies ALTER COLUMN pipeline_stage SET DEFAULT 'researching';
ALTER TABLE companies ALTER COLUMN pipeline_stage SET NOT NULL;

ALTER TABLE companies DROP CONSTRAINT IF EXISTS companies_pipeline_stage_check;
ALTER TABLE companies ADD CONSTRAINT companies_pipeline_stage_check
    CHECK (pipeline_stage IN (
        'researching', 'qualified', 'ready_for_outreach', 'contacted', 'replied',
        'discovery_scheduled', 'diagnostic_proposed', 'diagnostic_paid',
        'larger_engagement', 'won', 'lost', 'nurture'
    ));

CREATE INDEX IF NOT EXISTS idx_companies_pipeline_stage ON companies (pipeline_stage);
CREATE INDEX IF NOT EXISTS idx_companies_next_action_due_at ON companies (next_action_due_at);
CREATE INDEX IF NOT EXISTS idx_companies_owner ON companies (owner);

CREATE TABLE IF NOT EXISTS company_stage_history (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id UUID NOT NULL REFERENCES companies (id) ON DELETE CASCADE,
    from_stage TEXT NOT NULL,
    to_stage TEXT NOT NULL,
    changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    changed_by TEXT NOT NULL,
    reason TEXT,
    metadata JSONB
);

CREATE INDEX IF NOT EXISTS idx_company_stage_history_company_id
    ON company_stage_history (company_id);
CREATE INDEX IF NOT EXISTS idx_company_stage_history_changed_at
    ON company_stage_history (changed_at);

ALTER TABLE activities DROP CONSTRAINT IF EXISTS activities_activity_type_check;
ALTER TABLE activities ADD CONSTRAINT activities_activity_type_check
    CHECK (activity_type IN (
        'note', 'outreach', 'reply', 'meeting', 'proposal', 'payment',
        'task_completion', 'email', 'call', 'status_change'
    ));
"""

_REQUIRED = (os.environ.get("REQUIRE_TEST_DATABASE") or "").strip() in {"1", "true", "yes"}
_DATABASE_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip()


def _require_database_url() -> str:
    if _DATABASE_URL:
        return _DATABASE_URL
    if _REQUIRED:
        pytest.fail("REQUIRE_TEST_DATABASE=1 but TEST_DATABASE_URL is unset")
    pytest.skip("TEST_DATABASE_URL not set; skipping live Postgres migration tests")


@pytest.fixture(scope="module")
def database_url() -> str:
    return _require_database_url()


@contextmanager
def _connect(database_url: str) -> Iterator[psycopg.Connection]:
    # Default tuple rows: apply_migrations() expects fetchone()[0] access.
    conn = psycopg.connect(database_url, autocommit=False)
    try:
        yield conn
    finally:
        conn.close()


def _fetch_dicts(conn: psycopg.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return list(cur.fetchall())


def _fetch_dict(
    conn: psycopg.Connection, sql: str, params: tuple[Any, ...] = ()
) -> dict[str, Any] | None:
    rows = _fetch_dicts(conn, sql, params)
    return rows[0] if rows else None


def _reset_public_schema(conn: psycopg.Connection) -> None:
    """Wipe schema objects. apply_migrations() commits, so rollback alone is insufficient."""
    conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
    conn.execute("CREATE SCHEMA public")
    conn.execute("GRANT ALL ON SCHEMA public TO CURRENT_USER")
    conn.execute("GRANT ALL ON SCHEMA public TO public")
    conn.commit()


@pytest.fixture
def pg_conn(database_url: str) -> Iterator[psycopg.Connection]:
    """Isolated Postgres fixture; resets public schema around each test."""
    with _connect(database_url) as conn:
        _reset_public_schema(conn)
        try:
            yield conn
        finally:
            conn.rollback()
            _reset_public_schema(conn)


def _migrations_through(version: str) -> tuple[Migration, ...]:
    selected: list[Migration] = []
    for migration in MIGRATIONS:
        selected.append(migration)
        if migration.version == version:
            break
    else:
        raise AssertionError(f"migration {version} not found")
    return tuple(selected)


def _company_columns(conn: psycopg.Connection) -> set[str]:
    rows = _fetch_dicts(
        conn,
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'companies'
        """,
    )
    return {str(row["column_name"]) for row in rows}


def _table_exists(conn: psycopg.Connection, name: str) -> bool:
    row = _fetch_dict(conn, "SELECT to_regclass(%s) AS reg", (f"public.{name}",))
    assert row is not None
    return row["reg"] is not None


def _index_def(conn: psycopg.Connection, name: str) -> str | None:
    row = _fetch_dict(
        conn,
        "SELECT pg_get_indexdef(oid) AS def FROM pg_class WHERE relname = %s",
        (name,),
    )
    return None if row is None else str(row["def"])


def _schema_fingerprint(conn: psycopg.Connection) -> str:
    """Stable digest of pipeline-relevant schema + data for idempotency checks."""
    cols = sorted(_company_columns(conn))
    tables = sorted(
        str(row["tablename"])
        for row in _fetch_dicts(
            conn,
            """
            SELECT tablename FROM pg_tables
            WHERE schemaname = 'public'
              AND tablename IN (
                  'pipeline_stage_history', 'company_stage_history',
                  'import_batches', 'import_batch_rows', 'schema_migrations'
              )
            """,
        )
    )
    indexes = sorted(
        (
            str(row["indexname"]),
            str(row["indexdef"]),
        )
        for row in _fetch_dicts(
            conn,
            """
            SELECT indexname, indexdef
            FROM pg_indexes
            WHERE schemaname = 'public'
              AND indexname IN (
                  'idx_companies_pipeline_stage',
                  'idx_companies_next_action_due_at',
                  'idx_pipeline_stage_history_company_id'
              )
            """,
        )
    )
    history = (
        _fetch_dicts(
            conn,
            """
            SELECT id, company_id, from_stage, to_stage, changed_by, metadata
            FROM pipeline_stage_history
            ORDER BY id
            """,
        )
        if _table_exists(conn, "pipeline_stage_history")
        else []
    )
    companies = _fetch_dicts(
        conn,
        """
        SELECT id, pipeline_owner, expected_value_cents,
               pipeline_loss_reason, pipeline_nurture_reason, pipeline_stage
        FROM companies
        ORDER BY id
        """,
    )
    versions = [
        (str(row["version"]), str(row["name"]))
        for row in _fetch_dicts(
            conn,
            "SELECT version, name FROM schema_migrations ORDER BY version",
        )
    ]
    payload = json.dumps(
        {
            "cols": cols,
            "tables": tables,
            "indexes": indexes,
            "history": [
                {
                    "id": str(row["id"]),
                    "company_id": str(row["company_id"]),
                    "from_stage": row["from_stage"],
                    "to_stage": row["to_stage"],
                    "changed_by": row["changed_by"],
                    "metadata": row["metadata"],
                }
                for row in history
            ],
            "companies": [
                {
                    "id": str(row["id"]),
                    "pipeline_owner": row["pipeline_owner"],
                    "expected_value_cents": row["expected_value_cents"],
                    "pipeline_loss_reason": row["pipeline_loss_reason"],
                    "pipeline_nurture_reason": row["pipeline_nurture_reason"],
                    "pipeline_stage": row["pipeline_stage"],
                }
                for row in companies
            ],
            "versions": versions,
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _seed_legacy_pipeline_data(conn: psycopg.Connection) -> dict[str, Any]:
    company_id = uuid4()
    history_id = uuid4()
    conn.execute(
        """
        INSERT INTO companies (
            id, name, status, pipeline_stage, owner, expected_value, stage_reason
        )
        VALUES (%s, %s, 'prospect', 'lost', %s, %s, %s)
        """,
        (company_id, "Legacy Co", "Alex Owner", Decimal("2500.50"), "budget cut"),
    )
    conn.execute(
        """
        INSERT INTO companies (
            id, name, status, pipeline_stage, owner, expected_value, stage_reason
        )
        VALUES (%s, %s, 'prospect', 'nurture', %s, %s, %s)
        """,
        (uuid4(), "Nurture Co", "Sam Owner", Decimal("100.00"), "timing"),
    )
    conn.execute(
        """
        INSERT INTO company_stage_history (
            id, company_id, from_stage, to_stage, changed_by, reason, metadata
        )
        VALUES (%s, %s, 'qualified', 'lost', 'operator', 'budget cut', %s::jsonb)
        """,
        (history_id, company_id, json.dumps({"source": "manual"})),
    )
    return {"company_id": company_id, "history_id": history_id}


@pytest.mark.unit
def test_frozen_migration_digests_match_and_form_prefix() -> None:
    """Frozen digests must match SQL and be a contiguous prefix of the registry.

    Newest versions may remain unfrozen until post-deploy freezes them.
    """
    versions = [m.version for m in MIGRATIONS]
    by_version = {m.version: m for m in MIGRATIONS}
    frozen = FROZEN_MIGRATION_DIGESTS
    assert frozen
    assert set(frozen).issubset(by_version)
    frozen_in_order = [version for version in versions if version in frozen]
    assert frozen_in_order == versions[: len(frozen_in_order)]
    assert set(frozen_in_order) == set(frozen)
    for version, expected in frozen.items():
        assert migration_content_digest(by_version[version]) == expected


@pytest.mark.unit
def test_reconcile_migration_is_registered_after_import_batches() -> None:
    versions = [m.version for m in MIGRATIONS]
    assert versions[-1] == "015"
    reconcile = next(m for m in MIGRATIONS if m.name == "reconcile_acquisition_pipeline_schema")
    assert reconcile.version == "015"
    assert "pipeline_owner" in reconcile.up_sql
    assert "expected_value_cents" in reconcile.up_sql
    assert "pipeline_stage_history" in reconcile.up_sql
    assert "company_stage_history" in reconcile.up_sql
    assert "ON CONFLICT (id) DO NOTHING" in reconcile.up_sql
    assert "DROP INDEX IF EXISTS idx_companies_pipeline_stage" in reconcile.up_sql


@pytest.mark.integration
def test_legacy_013_upgrades_through_015(pg_conn: psycopg.Connection) -> None:
    applied = apply_migrations(pg_conn, migrations=_migrations_through("012"))
    assert applied[-1] == "012"

    pg_conn.execute(LEGACY_013_SQL)
    pg_conn.execute(
        """
        INSERT INTO schema_migrations (version, name)
        VALUES ('013', 'acquisition_pipeline')
        ON CONFLICT (version) DO NOTHING
        """
    )
    seeded = _seed_legacy_pipeline_data(pg_conn)

    assert "pipeline_owner" not in _company_columns(pg_conn)
    assert _table_exists(pg_conn, "company_stage_history")
    assert not _table_exists(pg_conn, "pipeline_stage_history")

    upgraded = apply_migrations(pg_conn)
    assert upgraded == ["014", "015"]

    columns = _company_columns(pg_conn)
    for name in (
        "pipeline_owner",
        "expected_value_cents",
        "pipeline_loss_reason",
        "pipeline_nurture_reason",
        "owner",
        "expected_value",
        "stage_reason",
    ):
        assert name in columns

    assert _table_exists(pg_conn, "pipeline_stage_history")
    assert _table_exists(pg_conn, "company_stage_history")

    lost = _fetch_dict(
        pg_conn,
        "SELECT * FROM companies WHERE id = %s",
        (seeded["company_id"],),
    )
    assert lost is not None
    assert lost["pipeline_owner"] == "Alex Owner"
    assert lost["expected_value_cents"] == 250_050
    assert lost["pipeline_loss_reason"] == "budget cut"
    assert lost["owner"] == "Alex Owner"
    assert lost["expected_value"] == Decimal("2500.50")

    nurture = _fetch_dict(
        pg_conn,
        "SELECT * FROM companies WHERE name = %s",
        ("Nurture Co",),
    )
    assert nurture is not None
    assert nurture["pipeline_owner"] == "Sam Owner"
    assert nurture["expected_value_cents"] == 10_000
    assert nurture["pipeline_nurture_reason"] == "timing"

    history = _fetch_dict(
        pg_conn,
        "SELECT * FROM pipeline_stage_history WHERE id = %s",
        (seeded["history_id"],),
    )
    assert history is not None
    assert history["company_id"] == seeded["company_id"]
    assert history["from_stage"] == "qualified"
    assert history["to_stage"] == "lost"
    assert history["changed_by"] == "operator"
    assert history["metadata"] == {"source": "manual", "legacy_reason": "budget cut"}

    stage_index = _index_def(pg_conn, "idx_companies_pipeline_stage")
    assert stage_index is not None
    assert "WHERE" in stage_index
    due_index = _index_def(pg_conn, "idx_companies_next_action_due_at")
    assert due_index is not None
    assert "archived_at" in due_index

    before = _schema_fingerprint(pg_conn)
    assert apply_migrations(pg_conn) == []
    # Re-run reconcile SQL only: history copy must stay idempotent.
    reconcile = next(m for m in MIGRATIONS if m.version == "015")
    pg_conn.execute(reconcile.up_sql)
    history_count = _fetch_dict(pg_conn, "SELECT COUNT(*) AS n FROM pipeline_stage_history")
    assert history_count is not None
    assert history_count["n"] == 1
    after = _schema_fingerprint(pg_conn)
    assert after == before

    pg_conn.row_factory = dict_row
    repo = PostgresPipelineRepository()
    updated = repo.update_pipeline_fields(
        pg_conn,
        seeded["company_id"],
        next_action="Follow up",
        pipeline_owner="Pat",
        expected_value_cents=99_00,
    )
    assert updated is not None
    assert updated["pipeline_owner"] == "Pat"
    assert updated["expected_value_cents"] == 99_00
    recorded = repo.record_stage_history(
        pg_conn,
        company_id=seeded["company_id"],
        from_stage="lost",
        to_stage="nurture",
        changed_by="operator",
        metadata={"note": "retry"},
    )
    assert recorded["to_stage"] == "nurture"
    listed = repo.list_stage_history(pg_conn, seeded["company_id"])
    assert any(row["id"] == recorded["id"] for row in listed)

    # Canonical loss/nurture reason *writes* (not only backfill reads).
    lost_write = repo.update_pipeline_fields(
        pg_conn,
        seeded["company_id"],
        pipeline_stage="lost",
        pipeline_loss_reason="no champion",
        clear_nurture_reason=True,
    )
    assert lost_write is not None
    assert lost_write["pipeline_stage"] == "lost"
    assert lost_write["pipeline_loss_reason"] == "no champion"
    assert lost_write["pipeline_nurture_reason"] is None

    nurture_write = repo.update_pipeline_fields(
        pg_conn,
        seeded["company_id"],
        pipeline_stage="nurture",
        pipeline_nurture_reason="revisit next quarter",
        clear_loss_reason=True,
    )
    assert nurture_write is not None
    assert nurture_write["pipeline_stage"] == "nurture"
    assert nurture_write["pipeline_nurture_reason"] == "revisit next quarter"
    assert nurture_write["pipeline_loss_reason"] is None


@pytest.mark.integration
def test_fresh_database_applies_001_through_015_idempotently(
    pg_conn: psycopg.Connection,
) -> None:
    first = apply_migrations(pg_conn)
    assert first == [m.version for m in MIGRATIONS]
    assert first[-1] == "015"

    columns = _company_columns(pg_conn)
    for name in (
        "pipeline_owner",
        "expected_value_cents",
        "pipeline_loss_reason",
        "pipeline_nurture_reason",
    ):
        assert name in columns
    assert _table_exists(pg_conn, "pipeline_stage_history")
    assert not _table_exists(pg_conn, "company_stage_history")

    before = _schema_fingerprint(pg_conn)
    second = apply_migrations(pg_conn)
    assert second == []
    assert _schema_fingerprint(pg_conn) == before

    company_id = uuid4()
    pg_conn.execute(
        """
        INSERT INTO companies (id, name, status, pipeline_stage)
        VALUES (%s, %s, 'prospect', 'researching')
        """,
        (company_id, "Fresh Co"),
    )
    pg_conn.row_factory = dict_row
    repo = PostgresPipelineRepository()
    repo.update_pipeline_fields(
        pg_conn,
        company_id,
        pipeline_stage="qualified",
        next_action="Send note",
        pipeline_owner="Alex",
        expected_value_cents=50_000,
    )
    repo.record_stage_history(
        pg_conn,
        company_id=company_id,
        from_stage="researching",
        to_stage="qualified",
        changed_by="alex",
    )
    assert repo.list_stage_history(pg_conn, company_id)


ACTOR = ActorContext(actor="operator", correlation_id="corr-reconcile-210")


def _upgrade_legacy_013(conn: psycopg.Connection) -> None:
    apply_migrations(conn, migrations=_migrations_through("012"))
    conn.execute(LEGACY_013_SQL)
    conn.execute(
        """
        INSERT INTO schema_migrations (version, name)
        VALUES ('013', 'acquisition_pipeline')
        ON CONFLICT (version) DO NOTHING
        """
    )
    assert apply_migrations(conn) == ["014", "015"]


def _insert_brief(
    conn: psycopg.Connection,
    *,
    website: str,
    email: str,
    status: str,
    brief: str = "Need architecture help.",
) -> dict[str, Any]:
    row = _fetch_dict(
        conn,
        """
        INSERT INTO project_briefs (website, contact_value, brief, status)
        VALUES (%s, %s, %s, %s)
        RETURNING *
        """,
        (website, email, brief, status),
    )
    assert row is not None
    return row


def _count(conn: psycopg.Connection, table: str) -> int:
    row = _fetch_dict(conn, f"SELECT COUNT(*) AS n FROM {table}")
    assert row is not None
    return int(row["n"])


@pytest.mark.integration
@pytest.mark.parametrize(
    ("status", "price_cents", "expected_stage", "expected_cents"),
    [
        ("paid", 20_000, "diagnostic_paid", 20_000),
        ("pending_payment", 0, "qualified", None),
    ],
)
def test_brief_conversion_on_legacy_013_reconciled_schema(
    pg_conn: psycopg.Connection,
    status: str,
    price_cents: int,
    expected_stage: str,
    expected_cents: int | None,
) -> None:
    _upgrade_legacy_013(pg_conn)
    brief = _insert_brief(
        pg_conn,
        website=f"https://{status.replace('_', '')}.example",
        email=f"{status}@example.com",
        status=status,
    )
    pg_conn.commit()

    pg_conn.row_factory = dict_row
    service = CrmService()
    first = service.convert_project_brief(
        pg_conn,
        brief=brief,
        actor_context=ACTOR,
        price_cents=price_cents,
        company_choice="new",
        contact_choice="new",
    )
    assert first["idempotent"] is False
    assert first["pipeline_stage"] == expected_stage
    company = first["company"]
    assert company["pipeline_stage"] == expected_stage
    assert company.get("expected_value_cents") == expected_cents
    assert company.get("pipeline_owner") is None or "pipeline_owner" in company

    source_count = _count(pg_conn, "source_records")
    company_count = _count(pg_conn, "companies")
    history_count = _count(pg_conn, "pipeline_stage_history")
    assert source_count == 1
    assert company_count == 1

    second = service.convert_project_brief(
        pg_conn,
        brief=brief,
        actor_context=ACTOR,
        price_cents=price_cents,
        company_choice="new",
        contact_choice="new",
    )
    assert second["idempotent"] is True
    assert second["company"]["id"] == first["company"]["id"]
    assert _count(pg_conn, "source_records") == source_count
    assert _count(pg_conn, "companies") == company_count
    assert _count(pg_conn, "pipeline_stage_history") == history_count


@pytest.mark.integration
def test_brief_conversion_rolls_back_on_reconciled_schema(
    pg_conn: psycopg.Connection,
) -> None:
    _upgrade_legacy_013(pg_conn)
    brief = _insert_brief(
        pg_conn,
        website="https://rollback.example",
        email="rollback@example.com",
        status="paid",
    )
    pg_conn.commit()

    before_companies = _count(pg_conn, "companies")
    before_sources = _count(pg_conn, "source_records")
    before_contacts = _count(pg_conn, "contacts")
    before_history = _count(pg_conn, "pipeline_stage_history")
    before_activities = _count(pg_conn, "activities")
    before_audit = _count(pg_conn, "audit_events")

    pg_conn.row_factory = dict_row
    service = CrmService()
    with patch(
        "app.crm_service.audit_service.record_brief_convert",
        side_effect=RuntimeError("audit failed"),
    ):
        with pytest.raises(RuntimeError, match="audit failed"):
            service.convert_project_brief(
                pg_conn,
                brief=brief,
                actor_context=ACTOR,
                price_cents=15_000,
                company_choice="new",
                contact_choice="new",
            )

    assert _count(pg_conn, "companies") == before_companies
    assert _count(pg_conn, "source_records") == before_sources
    assert _count(pg_conn, "contacts") == before_contacts
    assert _count(pg_conn, "pipeline_stage_history") == before_history
    assert _count(pg_conn, "activities") == before_activities
    assert _count(pg_conn, "audit_events") == before_audit
