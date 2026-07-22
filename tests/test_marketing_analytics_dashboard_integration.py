"""Integration tests for marketing analytics dashboard queries."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Iterator

import psycopg
import pytest
from psycopg.rows import dict_row

from app import analytics_ingest
from app.analytics_event_schema import ConsentState
from app.config import get_settings
from app.marketing_analytics_dashboard import load_marketing_analytics_dashboard
from app.repositories.postgres import PostgresMarketingAnalyticsRepository

UTC = timezone.utc
NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
VALID_SESSION = "550e8400-e29b-41d4-a716-446655440000"

_REQUIRED = (os.environ.get("REQUIRE_TEST_DATABASE") or "").strip() in {"1", "true", "yes"}
_DATABASE_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip()


def _require_database_url() -> str:
    if _DATABASE_URL:
        return _DATABASE_URL
    if _REQUIRED:
        pytest.fail("REQUIRE_TEST_DATABASE=1 but TEST_DATABASE_URL is unset")
    pytest.skip("TEST_DATABASE_URL not set; skipping marketing analytics integration tests")


@pytest.fixture(scope="module")
def database_url() -> str:
    return _require_database_url()


def _reset_public_schema(conn: psycopg.Connection) -> None:
    conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
    conn.execute("CREATE SCHEMA public")
    conn.execute("GRANT ALL ON SCHEMA public TO CURRENT_USER")
    conn.execute("GRANT ALL ON SCHEMA public TO public")
    conn.commit()


@pytest.fixture
def pg_conn(database_url: str) -> Iterator[psycopg.Connection]:
    from app.migrations.runner import apply_migrations

    with psycopg.connect(database_url, autocommit=False) as bootstrap:
        _reset_public_schema(bootstrap)
        apply_migrations(bootstrap)
    conn = psycopg.connect(database_url, row_factory=dict_row, autocommit=False)
    try:
        yield conn
    finally:
        conn.close()
        with psycopg.connect(database_url, autocommit=False) as cleanup:
            _reset_public_schema(cleanup)


def _insert_event(
    conn: psycopg.Connection,
    *,
    idempotency_key: str,
    event_name: str,
    occurred_at: datetime,
    properties: dict[str, object] | None = None,
    attribution: dict[str, str] | None = None,
) -> None:
    body = {
        "idempotency_key": idempotency_key,
        "event_name": event_name,
        "schema_version": "1.0.0",
        "occurred_at": occurred_at.isoformat(),
        "anonymous_session_id": VALID_SESSION,
        "path_class": "landing",
        "referrer_class": "direct",
        "attribution": attribution or {},
        "properties": properties or {"page": "/"},
        "consent_state": ConsentState.IMPLICIT_ANALYTICS.value,
    }
    settings = get_settings()
    result = analytics_ingest.ingest_browser_event(
        settings,
        raw_body=json.dumps(body).encode(),
        origin="http://testserver",
        referer=None,
        dnt_header=None,
        user_agent="Mozilla/5.0",
        source_key="127.0.0.1",
        conn=conn,
    )
    assert result.accepted


@pytest.mark.integration
def test_dashboard_counts_known_fixture_events(pg_conn: psycopg.Connection) -> None:
    window_start = datetime(2026, 7, 10, 0, 0, tzinfo=UTC)
    _insert_event(
        pg_conn,
        idempotency_key="a1",
        event_name="Landing Viewed",
        occurred_at=window_start,
        attribution={"utm_source": "linkedin", "utm_medium": "social", "utm_campaign": "launch"},
        properties={"page": "/", "funnel_step": 1},
    )
    _insert_event(
        pg_conn,
        idempotency_key="a2",
        event_name="Landing Viewed",
        occurred_at=window_start,
        attribution={"utm_source": "linkedin", "utm_medium": "social", "utm_campaign": "launch"},
        properties={"page": "/", "funnel_step": 1},
    )
    _insert_event(
        pg_conn,
        idempotency_key="a3",
        event_name="Brief Form Started",
        occurred_at=window_start,
        properties={"page": "/brief", "funnel_step": 4},
    )
    _insert_event(
        pg_conn,
        idempotency_key="a4",
        event_name="Insight Viewed",
        occurred_at=window_start,
        properties={"page": "/insights/postgres", "article_slug": "postgres-indexing"},
    )
    pg_conn.commit()

    data = load_marketing_analytics_dashboard(
        pg_conn,
        PostgresMarketingAnalyticsRepository(),
        date_from="2026-07-10",
        date_to="2026-07-10",
        now=NOW,
    )
    counts = {row.event_name: row.count for row in data.engagement_counts}
    assert counts["Landing Viewed"] == 2
    assert counts["Brief Form Started"] == 1
    assert data.article_engagement[0].slug == "postgres-indexing"
    assert data.article_engagement[0].views == 1
    assert data.conversion_rates[0].numerator == 1
    assert data.conversion_rates[0].denominator == 2
    assert data.conversion_rates[0].rate_pct == 50.0


@pytest.mark.integration
def test_duplicate_idempotency_key_not_double_counted(pg_conn: psycopg.Connection) -> None:
    occurred = datetime(2026, 7, 11, 8, 0, tzinfo=UTC)
    _insert_event(
        pg_conn,
        idempotency_key="dup-key",
        event_name="Services Viewed",
        occurred_at=occurred,
        properties={"page": "/services"},
    )
    _insert_event(
        pg_conn,
        idempotency_key="dup-key",
        event_name="Services Viewed",
        occurred_at=occurred,
        properties={"page": "/services"},
    )
    pg_conn.commit()

    data = load_marketing_analytics_dashboard(
        pg_conn,
        PostgresMarketingAnalyticsRepository(),
        date_from="2026-07-11",
        date_to="2026-07-11",
        now=NOW,
    )
    services = next(row for row in data.engagement_counts if row.event_name == "Services Viewed")
    assert services.count == 1
