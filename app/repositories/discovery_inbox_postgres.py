"""Postgres persistence for the lead discovery review inbox."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import psycopg

from app.discovery_inbox import (
    CONFIDENCE_FILTERS,
    DISCOVERY_FRESHNESS_FILTERS,
    DiscoveryInboxFilters,
    freshness_bucket,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _confidence_sql(bucket: str | None) -> tuple[str, list[Any]]:
    if bucket is None:
        return "", []
    if bucket == "high":
        return " AND confidence >= %s", [0.8]
    if bucket == "medium":
        return " AND confidence >= %s AND confidence < %s", [0.5, 0.8]
    return " AND (confidence IS NULL OR confidence < %s)", [0.5]


def _freshness_sql(bucket: str | None) -> tuple[str, list[Any]]:
    if bucket is None:
        return "", []
    now = _now()
    if bucket == "fresh":
        return " AND discovered_at >= %s", [now - timedelta(days=7)]
    if bucket == "recent":
        return (
            " AND discovered_at >= %s AND discovered_at < %s",
            [now - timedelta(days=30), now - timedelta(days=7)],
        )
    if bucket == "aging":
        return (
            " AND discovered_at >= %s AND discovered_at < %s",
            [now - timedelta(days=90), now - timedelta(days=30)],
        )
    return " AND discovered_at < %s", [now - timedelta(days=90)]


class PostgresDiscoveryInboxRepository:
    def list_runs(self, conn: psycopg.Connection, *, limit: int = 50) -> list[dict[str, Any]]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, source_id, started_at, completed_at, status, candidate_count
                FROM discovery_runs
                ORDER BY started_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            return [dict(row) for row in cur.fetchall()]

    def list_sources(self, conn: psycopg.Connection) -> list[str]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT source_id
                FROM discovery_candidates
                ORDER BY source_id ASC
                """
            )
            return [str(row["source_id"]) for row in cur.fetchall()]

    def list_candidates(
        self,
        conn: psycopg.Connection,
        *,
        filters: DiscoveryInboxFilters | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        filters = filters or DiscoveryInboxFilters()
        clauses = ["1=1"]
        params: list[Any] = []
        if filters.source:
            clauses.append("source_id = %s")
            params.append(filters.source)
        if filters.run_id:
            clauses.append("run_id = %s")
            params.append(filters.run_id)
        if filters.category:
            clauses.append("category = %s")
            params.append(filters.category)
        if filters.review_state:
            if filters.review_state == "deferred":
                clauses.append("review_state = 'deferred'")
            else:
                clauses.append("review_state = %s")
                params.append(filters.review_state)
        confidence_sql, confidence_params = _confidence_sql(filters.confidence)
        freshness_sql, freshness_params = _freshness_sql(filters.freshness)
        clauses.append("NOT EXISTS (")
        clauses.append(
            """
            SELECT 1 FROM discovery_rejection_suppressions s
            WHERE s.source_id = discovery_candidates.source_id
              AND s.external_id = discovery_candidates.external_id
              AND s.evidence_fingerprint = discovery_candidates.evidence_fingerprint
            """
        )
        clauses.append(")")
        where = " AND ".join(clauses) + confidence_sql + freshness_sql
        params.extend(confidence_params)
        params.extend(freshness_params)
        params.append(limit)
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT *
                FROM discovery_candidates
                WHERE {where}
                ORDER BY discovered_at DESC, created_at DESC
                LIMIT %s
                """,
                params,
            )
            rows = [dict(row) for row in cur.fetchall()]
        for row in rows:
            row["freshness"] = freshness_bucket(row.get("discovered_at"))
        return rows

    def get_candidate(
        self,
        conn: psycopg.Connection,
        candidate_id: UUID,
    ) -> dict[str, Any] | None:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM discovery_candidates WHERE id = %s",
                (candidate_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        data = dict(row)
        data["freshness"] = freshness_bucket(data.get("discovered_at"))
        return data

    def get_candidates_by_ids(
        self,
        conn: psycopg.Connection,
        candidate_ids: list[UUID],
    ) -> list[dict[str, Any]]:
        if not candidate_ids:
            return []
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM discovery_candidates
                WHERE id = ANY(%s)
                ORDER BY discovered_at DESC
                """,
                (candidate_ids,),
            )
            rows = [dict(row) for row in cur.fetchall()]
        for row in rows:
            row["freshness"] = freshness_bucket(row.get("discovered_at"))
        return rows

    def is_suppressed(
        self,
        conn: psycopg.Connection,
        *,
        source_id: str,
        external_id: str,
        evidence_fingerprint: str,
    ) -> bool:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1 FROM discovery_rejection_suppressions
                WHERE source_id = %s AND external_id = %s AND evidence_fingerprint = %s
                LIMIT 1
                """,
                (source_id, external_id, evidence_fingerprint),
            )
            return cur.fetchone() is not None

    def update_candidate_review(
        self,
        conn: psycopg.Connection,
        candidate_id: UUID,
        *,
        review_state: str,
        reviewed_by: str,
        linked_company_id: UUID | None = None,
        rejection_reason: str | None = None,
        deferred_until: datetime | None = None,
    ) -> dict[str, Any] | None:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE discovery_candidates
                SET review_state = %s,
                    reviewed_at = %s,
                    reviewed_by = %s,
                    linked_company_id = COALESCE(%s, linked_company_id),
                    rejection_reason = %s,
                    deferred_until = %s,
                    updated_at = %s
                WHERE id = %s
                RETURNING *
                """,
                (
                    review_state,
                    _now(),
                    reviewed_by,
                    linked_company_id,
                    rejection_reason,
                    deferred_until,
                    _now(),
                    candidate_id,
                ),
            )
            row = cur.fetchone()
        return dict(row) if row else None

    def record_rejection_suppression(
        self,
        conn: psycopg.Connection,
        *,
        source_id: str,
        external_id: str,
        evidence_fingerprint: str,
        rejection_reason: str,
        rejected_by: str,
        candidate_id: UUID,
    ) -> dict[str, Any]:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO discovery_rejection_suppressions (
                    source_id, external_id, evidence_fingerprint,
                    rejection_reason, rejected_by, candidate_id
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (source_id, external_id, evidence_fingerprint) DO UPDATE
                SET rejection_reason = EXCLUDED.rejection_reason,
                    rejected_at = EXCLUDED.rejected_at,
                    rejected_by = EXCLUDED.rejected_by,
                    candidate_id = EXCLUDED.candidate_id
                RETURNING *
                """,
                (
                    source_id,
                    external_id,
                    evidence_fingerprint,
                    rejection_reason,
                    rejected_by,
                    candidate_id,
                ),
            )
            row = cur.fetchone()
        return dict(row)

    def insert_candidate(
        self,
        conn: psycopg.Connection,
        *,
        run_id: UUID | None,
        source_id: str,
        external_id: str,
        evidence_fingerprint: str,
        name: str,
        domain: str | None = None,
        website: str | None = None,
        category: str | None = None,
        confidence: float | None = None,
        signals: list[str] | None = None,
        evidence: dict[str, Any] | None = None,
        raw_payload: dict[str, Any] | None = None,
        conflicts: dict[str, Any] | None = None,
        match_suggestions: dict[str, Any] | None = None,
        discovered_at: datetime | None = None,
    ) -> dict[str, Any]:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO discovery_candidates (
                    run_id, source_id, external_id, evidence_fingerprint,
                    name, domain, website, category, confidence, signals,
                    evidence, raw_payload, conflicts, match_suggestions, discovered_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, COALESCE(%s, NOW()))
                RETURNING *
                """,
                (
                    run_id,
                    source_id,
                    external_id,
                    evidence_fingerprint,
                    name,
                    domain,
                    website,
                    category,
                    confidence,
                    signals or [],
                    json.dumps(evidence) if evidence is not None else None,
                    json.dumps(raw_payload) if raw_payload is not None else None,
                    json.dumps(conflicts) if conflicts is not None else None,
                    json.dumps(match_suggestions) if match_suggestions is not None else None,
                    discovered_at,
                ),
            )
            row = cur.fetchone()
        return dict(row)


def discovery_filter_options() -> dict[str, dict[str, str]]:
    return {
        "freshness": DISCOVERY_FRESHNESS_FILTERS,
        "confidence": CONFIDENCE_FILTERS,
    }
