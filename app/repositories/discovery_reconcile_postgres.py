"""Postgres repositories for discovery reconciliation."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import psycopg


class PostgresDiscoveryReviewRepository:
    def count_pending(self, conn: psycopg.Connection) -> int:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) AS count
                FROM discovery_review_queue
                WHERE status = 'pending'
                """
            )
            row = cur.fetchone()
        return int(row["count"]) if row else 0

    def list_pending(
        self,
        conn: psycopg.Connection,
        *,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM discovery_review_queue
                WHERE status = 'pending'
                ORDER BY created_at ASC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()
        return [dict(row) for row in rows]

    def upsert_pending(
        self,
        conn: psycopg.Connection,
        *,
        external_id: str,
        source_id: str,
        candidate_name: str,
        candidate_domain: str | None,
        candidate_payload: dict[str, Any],
        reason: str,
        match_tier: str,
        candidate_company_ids: list[str],
    ) -> dict[str, Any]:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO discovery_review_queue (
                    external_id, source_id, candidate_name, candidate_domain,
                    candidate_payload, reason, match_tier, candidate_company_ids
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (external_id) WHERE status = 'pending'
                DO UPDATE SET
                    source_id = EXCLUDED.source_id,
                    candidate_name = EXCLUDED.candidate_name,
                    candidate_domain = EXCLUDED.candidate_domain,
                    candidate_payload = EXCLUDED.candidate_payload,
                    reason = EXCLUDED.reason,
                    match_tier = EXCLUDED.match_tier,
                    candidate_company_ids = EXCLUDED.candidate_company_ids,
                    updated_at = NOW()
                RETURNING *
                """,
                (
                    external_id,
                    source_id,
                    candidate_name,
                    candidate_domain,
                    json.dumps(candidate_payload),
                    reason,
                    match_tier,
                    json.dumps(candidate_company_ids),
                ),
            )
            row = cur.fetchone()
        return dict(row)

    def resolve(
        self,
        conn: psycopg.Connection,
        *,
        external_id: str,
        company_id: UUID | None,
        resolved_by: str,
    ) -> dict[str, Any] | None:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE discovery_review_queue
                SET status = 'resolved',
                    resolved_company_id = %s,
                    resolved_by = %s,
                    resolved_at = NOW(),
                    updated_at = NOW()
                WHERE external_id = %s AND status = 'pending'
                RETURNING *
                """,
                (company_id, resolved_by, external_id),
            )
            row = cur.fetchone()
        return dict(row) if row else None


class PostgresDiscoveryMergeDecisionRepository:
    def create(
        self,
        conn: psycopg.Connection,
        *,
        external_id: str,
        source_id: str,
        decision: str,
        company_id: UUID | None,
        candidate_domain: str | None,
        candidate_name: str,
        match_tier: str,
        actor: str,
        correlation_id: str | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO discovery_merge_decisions (
                    external_id, source_id, decision, company_id,
                    candidate_domain, candidate_name, match_tier,
                    actor, correlation_id, notes
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    external_id,
                    source_id,
                    decision,
                    company_id,
                    candidate_domain,
                    candidate_name,
                    match_tier,
                    actor,
                    correlation_id,
                    notes,
                ),
            )
            row = cur.fetchone()
        return dict(row)

    def get_latest(
        self,
        conn: psycopg.Connection,
        *,
        external_id: str,
    ) -> dict[str, Any] | None:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM discovery_merge_decisions
                WHERE external_id = %s
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (external_id,),
            )
            row = cur.fetchone()
        return dict(row) if row else None
