"""Postgres queries for the marketing analytics dashboard."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import psycopg

_ATTRIBUTION_DIMENSIONS = frozenset({"utm_source", "utm_medium", "utm_campaign"})


class PostgresAnalyticsDashboardRepository:
    def count_events_by_name(
        self,
        conn: psycopg.Connection,
        *,
        start: datetime,
        end: datetime,
        event_names: list[str],
    ) -> dict[str, int]:
        if not event_names:
            return {}
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT event_name, COUNT(*)::int AS total
                FROM analytics_events
                WHERE occurred_at >= %s
                  AND occurred_at < %s
                  AND event_name = ANY(%s)
                GROUP BY event_name
                """,
                (start, end, event_names),
            )
            rows = cur.fetchall()
        return {str(row["event_name"]): int(row["total"]) for row in rows}

    def count_crm_funnel(
        self,
        conn: psycopg.Connection,
        *,
        start: datetime,
        end: datetime,
    ) -> dict[str, int]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    COUNT(*) FILTER (
                        WHERE created_at >= %s AND created_at < %s
                    )::int AS leads,
                    COUNT(*) FILTER (
                        WHERE created_at >= %s
                          AND created_at < %s
                          AND stripe_session_id IS NOT NULL
                    )::int AS checkouts,
                    COUNT(*) FILTER (
                        WHERE status = 'paid'
                          AND paid_at >= %s
                          AND paid_at < %s
                    )::int AS payments
                FROM project_briefs
                """,
                (start, end, start, end, start, end),
            )
            row = cur.fetchone()
        return {
            "leads": int(row["leads"]),
            "checkouts": int(row["checkouts"]),
            "payments": int(row["payments"]),
        }

    def list_attribution_buckets(
        self,
        conn: psycopg.Connection,
        *,
        start: datetime,
        end: datetime,
        dimension: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        if dimension not in _ATTRIBUTION_DIMENSIONS:
            raise ValueError(f"unsupported attribution dimension: {dimension}")
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                    COALESCE(NULLIF(TRIM(attribution->>%s), ''), '(none)') AS key,
                    COUNT(*)::int AS event_count
                FROM analytics_events
                WHERE occurred_at >= %s
                  AND occurred_at < %s
                GROUP BY key
                ORDER BY event_count DESC, key ASC
                LIMIT %s
                """,
                (dimension, start, end, limit),
            )
            event_rows = cur.fetchall()
            cur.execute(
                f"""
                SELECT
                    COALESCE(NULLIF(TRIM({dimension}), ''), '(none)') AS key,
                    COUNT(*)::int AS lead_count
                FROM project_briefs
                WHERE created_at >= %s
                  AND created_at < %s
                GROUP BY key
                ORDER BY lead_count DESC, key ASC
                LIMIT %s
                """,
                (start, end, limit),
            )
            lead_rows = cur.fetchall()
        lead_map = {str(row["key"]): int(row["lead_count"]) for row in lead_rows}
        results: list[dict[str, Any]] = []
        for row in event_rows:
            key = str(row["key"])
            results.append(
                {
                    "key": key,
                    "event_count": int(row["event_count"]),
                    "lead_count": lead_map.get(key, 0),
                }
            )
        return results

    def list_content_engagement(
        self,
        conn: psycopg.Connection,
        *,
        start: datetime,
        end: datetime,
        event_name: str,
        slug_property: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        if slug_property not in {"case_study_slug", "article_slug"}:
            raise ValueError(f"unsupported slug property: {slug_property}")
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                    properties->>%s AS slug,
                    COUNT(*)::int AS views
                FROM analytics_events
                WHERE occurred_at >= %s
                  AND occurred_at < %s
                  AND event_name = %s
                  AND properties->>%s IS NOT NULL
                  AND TRIM(properties->>%s) <> ''
                GROUP BY slug
                ORDER BY views DESC, slug ASC
                LIMIT %s
                """,
                (slug_property, start, end, event_name, slug_property, slug_property, limit),
            )
            rows = cur.fetchall()
        return [dict(row) for row in rows]
