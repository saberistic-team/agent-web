"""Tests for the marketing analytics admin dashboard (#116)."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Generator
from unittest.mock import MagicMock, patch

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app.admin_analytics_pages import render_marketing_analytics_page
from app.admin_auth import SESSION_COOKIE_NAME
from app.admin_preview import build_preview_marketing_analytics_data
from app.main import app
from app.marketing_analytics_dashboard import (
    ContentEngagementRow,
    ConversionRateRow,
    EventAttributionRow,
    EventCountRow,
    LeadAttributionRow,
    MarketingAnalyticsDashboardData,
    parse_analytics_date_range,
)

client = TestClient(app, follow_redirects=False)

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "test-session-secret-32chars-minimum"
TEST_LIMITER_SECRET = "test-limiter-secret-32chars-minimum!!"
NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")


@contextmanager
def mock_db_connection() -> Generator[MagicMock, None, None]:
    conn = MagicMock()
    with patch("app.admin_routes.db.db_connection") as db_conn:
        db_conn.return_value.__enter__.return_value = conn
        yield conn


def _session_row(*, token_hash: str) -> dict[str, Any]:
    return {
        "id": 1,
        "token_hash": token_hash,
        "admin_username": TEST_USERNAME,
        "csrf_token_hash": None,
        "expires_at": datetime(2099, 1, 1, tzinfo=timezone.utc),
    }


def _populated_dashboard() -> MarketingAnalyticsDashboardData:
    date_range = parse_analytics_date_range(reference=NOW)
    return MarketingAnalyticsDashboardData(
        date_range=date_range,
        engagement_events=(
            EventCountRow("Landing Viewed", 120, "browser"),
            EventCountRow("Services Viewed", 45, "browser"),
            EventCountRow("Brief Form Started", 18, "browser"),
            EventCountRow("Contact Initiated", 6, "browser"),
        ),
        server_conversion_events=(
            EventCountRow("Lead Persisted", 7, "server"),
            EventCountRow("Checkout Opened", 5, "server"),
            EventCountRow("Payment Completed", 3, "server"),
        ),
        client_supplementary_events=(
            EventCountRow("Brief Success Viewed", 2, "client_supplementary"),
        ),
        conversion_rates=(
            ConversionRateRow(
                key="form_to_lead",
                label="Brief form → lead persisted",
                numerator_event="Lead Persisted",
                denominator_event="Brief Form Started",
                numerator=7,
                denominator=18,
                rate_pct=38.9,
                definition=(
                    "Numerator: count of `Lead Persisted` in window. "
                    "Denominator: count of `Brief Form Started` in window."
                ),
            ),
        ),
        event_attribution=(
            EventAttributionRow("linkedin", "social", "spring-launch", 55),
        ),
        lead_attribution=(
            LeadAttributionRow("linkedin", "social", "spring-launch", 4, 2),
        ),
        case_study_engagement=(ContentEngagementRow("payments-platform", 22),),
        article_engagement=(ContentEngagementRow("diagnostic-playbook", 14),),
        generated_at=NOW,
    )


@pytest.mark.unit
def test_render_marketing_analytics_page_includes_sections() -> None:
    html = render_marketing_analytics_page(
        data=_populated_dashboard(),
        admin_username=TEST_USERNAME,
    )
    assert "Marketing analytics" in html
    assert "Page &amp; engagement" in html
    assert "Authoritative conversions" in html
    assert "Client UX signals" in html
    assert "Conversion rates" in html
    assert "Event attribution" in html
    assert "Lead &amp; payment attribution" in html
    assert "Case study views" in html
    assert "Insight views" in html
    assert "Export CSV" in html
    assert "payments-platform" in html


@pytest.mark.unit
def test_render_marketing_analytics_zero_denominator_shows_dash() -> None:
    data = _populated_dashboard()
    zero_rate = ConversionRateRow(
        key="checkout_to_payment",
        label="Checkout → payment completed",
        numerator_event="Payment Completed",
        denominator_event="Checkout Opened",
        numerator=0,
        denominator=0,
        rate_pct=None,
        definition="test",
    )
    updated = MarketingAnalyticsDashboardData(
        date_range=data.date_range,
        engagement_events=data.engagement_events,
        server_conversion_events=data.server_conversion_events,
        client_supplementary_events=data.client_supplementary_events,
        conversion_rates=(zero_rate,),
        event_attribution=data.event_attribution,
        lead_attribution=data.lead_attribution,
        case_study_engagement=data.case_study_engagement,
        article_engagement=data.article_engagement,
        generated_at=data.generated_at,
    )
    html = render_marketing_analytics_page(data=updated, admin_username=TEST_USERNAME)
    assert ">—<" in html


@pytest.mark.unit
@pytest.mark.integration
def test_admin_analytics_requires_auth() -> None:
    response = client.get("/admin/analytics")
    assert response.status_code == 303
    assert "/admin/login" in response.headers["location"]


@pytest.mark.unit
@pytest.mark.integration
def test_admin_analytics_populated_state() -> None:
    from app import admin_auth

    raw_token = admin_auth.generate_session_token()
    token_hash = admin_auth.hash_session_token(raw_token)
    row = _session_row(token_hash=token_hash)
    with mock_db_connection():
        with (
            patch(
                "app.admin_routes.db.get_admin_session_by_token_hash",
                return_value=row,
            ),
            patch(
                "app.admin_routes.load_marketing_analytics_dashboard",
                return_value=_populated_dashboard(),
            ),
        ):
            response = client.get("/admin/analytics", cookies={SESSION_COOKIE_NAME: raw_token})
    assert response.status_code == 200
    assert 'id="analytics-title"' in response.text
    assert "Marketing analytics" in response.text
    assert "linkedin" in response.text


@pytest.mark.unit
@pytest.mark.integration
def test_admin_analytics_csv_export() -> None:
    from app import admin_auth

    raw_token = admin_auth.generate_session_token()
    token_hash = admin_auth.hash_session_token(raw_token)
    row = _session_row(token_hash=token_hash)
    with mock_db_connection():
        with (
            patch(
                "app.admin_routes.db.get_admin_session_by_token_hash",
                return_value=row,
            ),
            patch(
                "app.admin_routes.load_marketing_analytics_dashboard",
                return_value=_populated_dashboard(),
            ),
            patch("app.admin_routes._crm.request_export") as export_audit,
        ):
            response = client.get(
                "/admin/analytics/export.csv",
                cookies={SESSION_COOKIE_NAME: raw_token},
            )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "Landing Viewed" in response.text
    assert "anonymous_session_id" not in response.text
    export_audit.assert_called_once()


@pytest.mark.unit
def test_preview_marketing_analytics_seed_stable() -> None:
    import random

    a = build_preview_marketing_analytics_data(rng=random.Random(42), now=NOW)
    b = build_preview_marketing_analytics_data(rng=random.Random(42), now=NOW)
    assert a == b
    assert len(a.engagement_events) > 0
    assert len(a.conversion_rates) == 5
