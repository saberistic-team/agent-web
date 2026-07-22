"""Postgres persistence for discovery runs, per-source outcomes, and checkpoints."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import psycopg

from app.discovery.types import DiscoveryCheckpoint


class PostgresDiscoveryRunRepository:
    def create_run(
        self,
        conn: psycopg.Connection,
        *,
        trigger_type: str,
        status: str,
        correlation_id: str,
        enabled_sources: list[str],
        actor: str | None = None,
        lock_acquired: bool = False,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO discovery_runs (
                    trigger_type, status, actor, correlation_id,
                    enabled_sources, lock_acquired, error_message
                )
                VALUES (%s, %s, %s, %s, %s::text[], %s, %s)
                RETURNING *
                """,
                (
                    trigger_type,
                    status,
                    actor,
                    correlation_id,
                    enabled_sources,
                    lock_acquired,
                    error_message,
                ),
            )
            row = cur.fetchone()
        return dict(row)

    def finish_run(
        self,
        conn: psycopg.Connection,
        run_id: UUID,
        *,
        status: str,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE discovery_runs
                SET status = %s,
                    finished_at = NOW(),
                    error_message = %s
                WHERE id = %s
                RETURNING *
                """,
                (status, error_message, run_id),
            )
            row = cur.fetchone()
        return dict(row)

    def get_by_id(self, conn: psycopg.Connection, run_id: UUID) -> dict[str, Any] | None:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM discovery_runs WHERE id = %s", (run_id,))
            row = cur.fetchone()
        return dict(row) if row else None

    def list_page(
        self,
        conn: psycopg.Connection,
        *,
        page: int = 1,
        per_page: int = 50,
    ) -> tuple[list[dict[str, Any]], int]:
        safe_page = max(page, 1)
        safe_per_page = max(min(per_page, 100), 1)
        offset = (safe_page - 1) * safe_per_page
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS total FROM discovery_runs")
            total_row = cur.fetchone()
            total = int(total_row["total"]) if total_row else 0
            cur.execute(
                """
                SELECT * FROM discovery_runs
                ORDER BY started_at DESC
                LIMIT %s OFFSET %s
                """,
                (safe_per_page, offset),
            )
            rows = cur.fetchall()
        return [dict(row) for row in rows], total

    def latest_scheduled_started_at(
        self, conn: psycopg.Connection
    ) -> str | None:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT started_at FROM discovery_runs
                WHERE trigger_type = 'scheduled'
                  AND status IN ('completed', 'partial', 'failed')
                ORDER BY started_at DESC
                LIMIT 1
                """
            )
            row = cur.fetchone()
        if row is None:
            return None
        started_at = row["started_at"]
        return started_at.isoformat() if hasattr(started_at, "isoformat") else str(started_at)

    def create_source_result(
        self,
        conn: psycopg.Connection,
        *,
        run_id: UUID,
        source_id: str,
        status: str,
        fetched_count: int,
        accepted_count: int,
        rejected_count: int,
        error_count: int,
        checkpoint: DiscoveryCheckpoint | None,
        errors: list[dict[str, object]],
    ) -> dict[str, Any]:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO discovery_run_sources (
                    run_id, source_id, status, finished_at,
                    fetched_count, accepted_count, rejected_count, error_count,
                    checkpoint_cursor, checkpoint_etag, checkpoint_last_modified,
                    checkpoint_last_run_at, errors
                )
                VALUES (
                    %s, %s, %s, NOW(),
                    %s, %s, %s, %s,
                    %s, %s, %s, %s, %s::jsonb
                )
                RETURNING *
                """,
                (
                    run_id,
                    source_id,
                    status,
                    fetched_count,
                    accepted_count,
                    rejected_count,
                    error_count,
                    checkpoint.cursor if checkpoint else None,
                    checkpoint.etag if checkpoint else None,
                    checkpoint.last_modified if checkpoint else None,
                    checkpoint.last_run_at if checkpoint else None,
                    json.dumps(errors),
                ),
            )
            row = cur.fetchone()
        return dict(row)

    def list_sources_for_run(
        self, conn: psycopg.Connection, run_id: UUID
    ) -> list[dict[str, Any]]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM discovery_run_sources
                WHERE run_id = %s
                ORDER BY source_id ASC
                """,
                (run_id,),
            )
            rows = cur.fetchall()
        return [dict(row) for row in rows]

    def load_checkpoints(
        self, conn: psycopg.Connection
    ) -> dict[str, DiscoveryCheckpoint]:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM discovery_source_checkpoints")
            rows = cur.fetchall()
        checkpoints: dict[str, DiscoveryCheckpoint] = {}
        for row in rows:
            checkpoints[str(row["source_id"])] = DiscoveryCheckpoint(
                cursor=row.get("cursor"),
                last_run_at=row.get("last_run_at"),
                etag=row.get("etag"),
                last_modified=row.get("last_modified"),
            )
        return checkpoints

    def upsert_checkpoint(
        self,
        conn: psycopg.Connection,
        *,
        source_id: str,
        checkpoint: DiscoveryCheckpoint,
        success: bool,
    ) -> None:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO discovery_source_checkpoints (
                    source_id, cursor, etag, last_modified, last_run_at,
                    last_success_at, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, CASE WHEN %s THEN NOW() ELSE NULL END, NOW())
                ON CONFLICT (source_id) DO UPDATE SET
                    cursor = EXCLUDED.cursor,
                    etag = EXCLUDED.etag,
                    last_modified = EXCLUDED.last_modified,
                    last_run_at = EXCLUDED.last_run_at,
                    last_success_at = CASE
                        WHEN EXCLUDED.last_success_at IS NOT NULL THEN EXCLUDED.last_success_at
                        ELSE discovery_source_checkpoints.last_success_at
                    END,
                    updated_at = NOW()
                """,
                (
                    source_id,
                    checkpoint.cursor,
                    checkpoint.etag,
                    checkpoint.last_modified,
                    checkpoint.last_run_at,
                    success,
                ),
            )
