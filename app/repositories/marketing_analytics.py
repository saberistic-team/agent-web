"""Postgres queries for the marketing analytics dashboard."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import psycopg

from app.analytics_event_schema import UTM_ATTRIBUTION_KEYS


class PostgresMarketingAnalyticsRepository:
    """Indexed, bounded analytics rollups over analytics_events and project_briefs."""

    def count_events_by_name(
        self,
        conn: psycopg.Connection,
        *,
        start: datetime,
        end_exclusive: datetime,
        event_names: tuple[str, ...],
    ) -> list[tuple[str, int]]:
        if not event_names:
            return []
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT event_name, COUNT(*)::int AS total
                FROM analytics_events
                WHERE occurred_at >= %s
                  AND occurred_at < %s
                  AND event_name = ANY(%s)
                GROUP BY event_name
                ORDER BY total DESC, event_name ASC
                """,
                (start, end_exclusive, list(event_names)),
            )
            rows = cur.fetchall()
        return [(str(row["event_name"]), int(row["total"])) for row in rows]

    def count_content_views(
        self,
        conn: psycopg.Connection,
        *,
        start: datetime,
        end_exclusive: datetime,
        event_name: str,
        slug_property: str,
        limit: int,
    ) -> list[tuple[str, int]]:
        if slug_property not in {"case_study_slug", "article_slug"}:
            raise ValueError(f"unsupported slug property: {slug_property}")
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT properties->>%s AS slug, COUNT(*)::int AS views
                FROM analytics_events
                WHERE occurred_at >= %s
                  AND occurred_at < %s
                  AND event_name = %s
                  AND properties ? %s
                  AND COALESCE(properties->>%s, '') <> ''
                GROUP BY slug
                ORDER BY views DESC, slug ASC
                LIMIT %s
                """,
                (
                    slug_property,
                    start,
                    end_exclusive,
                    event_name,
                    slug_property,
                    slug_property,
                    limit,
                ),
            )
            rows = cur.fetchall()
        return [(str(row["slug"]), int(row["views"])) for row in rows if row.get("slug")]

    def count_engagement_attribution(
        self,
        conn: psycopg.Connection,
        *,
        start: datetime,
        end_exclusive: datetime,
        limit: int,
    ) -> list[dict[str, Any]]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                  COALESCE(attribution->>'utm_source', '(direct)') AS utm_source,
                  COALESCE(attribution->>'utm_medium', '(none)') AS utm_medium,
                  COALESCE(attribution->>'utm_campaign', '(none)') AS utm_campaign,
                  COUNT(*)::int AS engagement_events
                FROM analytics_events
                WHERE occurred_at >= %s
                  AND occurred_at < %s
                GROUP BY 1, 2, 3
                ORDER BY engagement_events DESC, utm_source ASC
                LIMIT %s
                """,
                (start, end_exclusive, limit),
            )
            rows = cur.fetchall()
        return [dict(row) for row in rows]

    def count_brief_attribution(
        self,
        conn: psycopg.Connection,
        *,
        start: datetime,
        end_exclusive: datetime,
        limit: int,
    ) -> list[dict[str, Any]]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                  COALESCE(utm_source, '(direct)') AS utm_source,
                  COALESCE(utm_medium, '(none)') AS utm_medium,
                  COALESCE(utm_campaign, '(none)') AS utm_campaign,
                  COUNT(*)::int AS leads,
                  COUNT(*) FILTER (WHERE status = 'paid')::int AS payments
                FROM project_briefs
                WHERE created_at >= %s
                  AND created_at < %s
                GROUP BY 1, 2, 3
                ORDER BY leads DESC, utm_source ASC
                LIMIT %s
                """,
                (start, end_exclusive, limit),
            )
            rows = cur.fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def allowed_attribution_keys() -> frozenset[str]:
        return UTM_ATTRIBUTION_KEYS
