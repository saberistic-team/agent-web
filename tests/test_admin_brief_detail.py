"""Tests for the read-only admin brief detail page (#147)."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Generator
from unittest.mock import MagicMock, patch

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app import admin_auth, brief_service
from app.admin_auth import SESSION_COOKIE_NAME
from app.admin_pages import (
    render_admin_brief_database_unavailable,
    render_admin_brief_detail_page,
    render_admin_brief_not_found,
)
from app.brief_service import BriefListFilters
from app.main import app
from app.repositories.postgres import PostgresProjectBriefRepository

client = TestClient(app, follow_redirects=False)

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "test-session-secret-32chars-minimum"


@pytest.fixture(autouse=True)
def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    admin_auth.reset_login_rate_limiter()


@contextmanager
def mock_db_connection() -> Generator[MagicMock, None, None]:
    conn = MagicMock()
    with patch("app.admin_routes.db.db_connection") as db_conn:
        db_conn.return_value.__enter__.return_value = conn
        db_conn.return_value.__exit__.return_value = None
        yield conn


def _session_row(*, token_hash: str) -> dict[str, Any]:
    return {
        "id": 1,
        "token_hash": token_hash,
        "admin_username": TEST_USERNAME,
        "created_at": datetime.now(timezone.utc),
        "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
        "revoked_at": None,
        "csrf_token_hash": None,
    }


def _detail_brief() -> dict[str, Any]:
    return {
        "id": 42,
        "created_at": datetime(2026, 7, 14, 10, 30, tzinfo=timezone.utc),
        "website": "https://acme.example",
        "contact_method": "email",
        "contact_value": "ops@acme.example",
        "brief": "Need architecture review for payments platform.",
        "status": "paid",
        "stripe_session_id": "cs_test_session_secret",
        "stripe_payment_intent_id": "pi_test_intent_secret",
        "paid_at": datetime(2026, 7, 14, 10, 45, tzinfo=timezone.utc),
        "utm_source": "linkedin",
        "utm_medium": "social",
        "utm_campaign": "spring-launch",
        "utm_content": "cta",
        "utm_term": "arch",
    }


def _back_filters() -> BriefListFilters:
    return BriefListFilters(
        page=2,
        per_page=50,
        query="acme",
        status="paid",
        date_from=None,
        date_to=None,
        date_from_raw="2026-07-01",
        date_to_raw="2026-07-14",
    )


@pytest.mark.unit
def test_parse_brief_id_accepts_valid_positive_integers() -> None:
    assert brief_service.parse_brief_id("1") == 1
    assert brief_service.parse_brief_id("42") == 42
    assert brief_service.parse_brief_id("999999999") == 999999999
    assert brief_service.parse_brief_id(str(brief_service.MAX_BRIEF_ID)) == brief_service.MAX_BRIEF_ID


@pytest.mark.unit
def test_parse_brief_id_rejects_invalid_identifiers() -> None:
    assert brief_service.parse_brief_id("") is None
    assert brief_service.parse_brief_id("0") is None
    assert brief_service.parse_brief_id("-1") is None
    assert brief_service.parse_brief_id("-") is None
    assert brief_service.parse_brief_id("not-a-number") is None
    assert brief_service.parse_brief_id("42x") is None
    assert brief_service.parse_brief_id("2147483648") is None
    assert brief_service.parse_brief_id("999999999999999999") is None


@pytest.mark.unit
def test_get_brief_delegates_to_repository() -> None:
    conn = MagicMock()
    repo = MagicMock()
    repo.get_by_id.return_value = {"id": 9}
    brief = brief_service.get_brief(conn, 9, repository=repo)
    assert brief == {"id": 9}
    repo.get_by_id.assert_called_once_with(conn, 9)


@pytest.mark.unit
def test_get_brief_rejects_invalid_ids() -> None:
    conn = MagicMock()
    repo = MagicMock()
    assert brief_service.get_brief(conn, 0, repository=repo) is None
    repo.get_by_id.assert_not_called()


@pytest.mark.unit
def test_normalize_list_back_params_reuses_list_validation() -> None:
    filters = brief_service.normalize_list_back_params(
        page=0,
        q="  acme  ",
        status="paid",
        date_from="2026-07-01",
        date_to="bad",
    )
    assert filters.page == 1
    assert filters.query == "acme"
    assert filters.status == "paid"
    assert filters.date_from_raw == "2026-07-01"


@pytest.mark.unit
def test_postgres_project_brief_repository_get_by_id_selects_detail_columns() -> None:
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    cursor.fetchone.return_value = {"id": 3, "brief": "text"}
    repo = PostgresProjectBriefRepository()
    row = repo.get_by_id(conn, 3)
    assert row == {"id": 3, "brief": "text"}
    sql = cursor.execute.call_args[0][0]
    assert "stripe_session_id" in sql
    assert "payment_amount_cents" in sql
    assert "stripe_promotion_code_id" in sql
    assert "utm_term" in sql
    assert "WHERE id = %s" in sql


@pytest.mark.unit
def test_render_admin_brief_detail_page_shows_discounted_payment_breakdown() -> None:
    brief = _detail_brief()
    brief["payment_subtotal_cents"] = 20_000
    brief["payment_discount_cents"] = 5_000
    brief["payment_amount_cents"] = 15_000
    brief["payment_currency"] = "usd"
    brief["stripe_promotion_code_id"] = "promo_test_detail"
    html_out = render_admin_brief_detail_page(
        admin_username=TEST_USERNAME,
        brief=brief,
        back_filters=_back_filters(),
        price_cents=20_000,
    )
    assert "Subtotal: $200" in html_out
    assert "Discount: −$50" in html_out
    assert "Total collected: $150" in html_out
    assert "Currency: USD" in html_out
    assert "promo_test_detail" in html_out


@pytest.mark.unit
def test_render_admin_brief_detail_page_escapes_html_and_wraps_content() -> None:
    brief = _detail_brief()
    brief["website"] = '"><img onerror=alert(1) src=x>'
    brief["brief"] = "A" * 300 + "\n<script>evil()</script>"
    html_out = render_admin_brief_detail_page(
        admin_username=TEST_USERNAME,
        brief=brief,
        back_filters=_back_filters(),
        price_cents=20_000,
    )
    assert "<script>" not in html_out
    assert "&lt;script&gt;" in html_out or "&lt;img" in html_out
    assert "brief-detail-text" in html_out
    assert "brief-detail-url" in html_out
    assert 'meta name="robots" content="noindex, nofollow"' in html_out
    assert "<title>Brief #42 · saberistic admin</title>" in html_out
    assert "cs_test_session_secret" in html_out
    assert "pi_test_intent_secret" in html_out
    title_section = html_out.split("</title>", maxsplit=1)[0]
    assert "cs_test_session_secret" not in title_section
    assert "pi_test_intent_secret" not in title_section


@pytest.mark.unit
def test_render_admin_brief_detail_page_shows_nullable_payment_and_utm_fields() -> None:
    brief = _detail_brief()
    brief["status"] = "pending_payment"
    brief["stripe_session_id"] = None
    brief["stripe_payment_intent_id"] = None
    brief["paid_at"] = None
    brief["utm_source"] = None
    brief["utm_medium"] = None
    brief["utm_campaign"] = None
    brief["utm_content"] = None
    brief["utm_term"] = None
    html_out = render_admin_brief_detail_page(
        admin_username=TEST_USERNAME,
        brief=brief,
        back_filters=BriefListFilters(
            page=1,
            per_page=50,
            query=None,
            status=None,
            date_from=None,
            date_to=None,
            date_from_raw=None,
            date_to_raw=None,
        ),
        price_cents=20_000,
    )
    assert "Stripe references" not in html_out
    assert "audit-muted" in html_out
    assert "Pending" in html_out


@pytest.mark.unit
def test_render_admin_brief_detail_page_preserves_safe_back_navigation() -> None:
    html_out = render_admin_brief_detail_page(
        admin_username=TEST_USERNAME,
        brief=_detail_brief(),
        back_filters=_back_filters(),
        price_cents=20_000,
    )
    assert "Back to briefs" in html_out
    assert "page=2" in html_out
    assert "q=acme" in html_out
    assert "status=paid" in html_out
    assert "date_from=2026-07-01" in html_out
    assert "date_to=2026-07-14" in html_out


@pytest.mark.unit
def test_render_admin_brief_detail_page_ignores_unsafe_back_params() -> None:
    filters = brief_service.normalize_list_back_params(
        page=1,
        q="javascript:alert(1)",
        status="hacked",
        date_from="not-a-date",
    )
    html_out = render_admin_brief_detail_page(
        admin_username=TEST_USERNAME,
        brief=_detail_brief(),
        back_filters=filters,
        price_cents=20_000,
    )
    assert "javascript:" not in html_out
    assert "status=hacked" not in html_out


@pytest.mark.unit
def test_render_admin_brief_not_found_includes_back_link() -> None:
    html_out = render_admin_brief_not_found(
        brief_id=99,
        admin_username=TEST_USERNAME,
        back_filters=_back_filters(),
    )
    assert "Brief not found" in html_out
    assert "No project brief exists with ID #99" in html_out
    assert "page=2" in html_out
    assert 'meta name="robots" content="noindex, nofollow"' in html_out


@pytest.mark.unit
def test_render_admin_brief_database_unavailable_includes_retry_and_back_link() -> None:
    html_out = render_admin_brief_database_unavailable(
        admin_username=TEST_USERNAME,
        back_filters=_back_filters(),
        retry_href="/admin/briefs/42?page=2&q=acme",
        correlation_id="corr-test-42",
    )
    assert "Briefs temporarily unavailable" in html_out
    assert "Could not load this brief from the database." in html_out
    assert "temporarily unavailable" in html_out
    assert 'href="/admin/briefs/42?page=2&amp;q=acme"' in html_out
    assert "Retry" in html_out
    assert "page=2" in html_out
    assert "corr-test-42" in html_out
    assert "SELECT" not in html_out
    assert "postgresql://" not in html_out


@pytest.mark.unit
@pytest.mark.integration
def test_admin_brief_detail_anonymous_malformed_ids_redirect_to_login() -> None:
    for path in (
        "/admin/briefs/not-a-number",
        "/admin/briefs/0",
        "/admin/briefs/-5",
        "/admin/briefs/999999999",
        "/admin/briefs/2147483648",
    ):
        response = client.get(path)
        assert response.status_code == 303, path
        assert "/admin/login" in response.headers["location"], path
        assert "application/json" not in response.headers.get("content-type", ""), path


@pytest.mark.unit
@pytest.mark.integration
def test_admin_brief_detail_requires_auth() -> None:
    response = client.get("/admin/briefs/42")
    assert response.status_code == 303
    assert "/admin/login" in response.headers["location"]


@pytest.mark.unit
@pytest.mark.integration
def test_admin_brief_detail_renders_record() -> None:
    token_hash = admin_auth.hash_session_token("detail-session")
    row = _session_row(token_hash=token_hash)
    with mock_db_connection() as conn:
        with patch("app.admin_routes.db.get_admin_session_by_token_hash", return_value=row):
            with patch(
                "app.admin_routes.brief_service.get_brief",
                return_value=_detail_brief(),
            ) as get_brief:
                response = client.get(
                    "/admin/briefs/42?page=2&q=acme&status=paid",
                    cookies={SESSION_COOKIE_NAME: "detail-session"},
                )
    assert response.status_code == 200
    body = response.text
    assert "Project brief #42" in body
    assert "Need architecture review for payments platform." in body
    assert "ops@acme.example" in body
    assert "linkedin" in body
    assert "Back to briefs" in body
    assert "page=2" in body
    assert "q=acme" in body
    get_brief.assert_called_once_with(conn, 42)


@pytest.mark.unit
@pytest.mark.integration
def test_admin_brief_detail_missing_record_returns_authenticated_404() -> None:
    token_hash = admin_auth.hash_session_token("detail-missing")
    row = _session_row(token_hash=token_hash)
    with mock_db_connection():
        with patch("app.admin_routes.db.get_admin_session_by_token_hash", return_value=row):
            with patch("app.admin_routes.brief_service.get_brief", return_value=None):
                response = client.get(
                    "/admin/briefs/999",
                    cookies={SESSION_COOKIE_NAME: "detail-missing"},
                )
    assert response.status_code == 404
    assert "Brief not found" in response.text


@pytest.mark.unit
@pytest.mark.integration
def test_admin_brief_detail_database_error_returns_authenticated_503_without_leak() -> None:
    import psycopg

    token_hash = admin_auth.hash_session_token("detail-db-error")
    row = _session_row(token_hash=token_hash)
    with mock_db_connection():
        with patch("app.admin_routes.db.get_admin_session_by_token_hash", return_value=row):
            with patch(
                "app.admin_routes.brief_service.get_brief",
                side_effect=psycopg.OperationalError("connection refused"),
            ):
                response = client.get(
                    "/admin/briefs/42",
                    cookies={SESSION_COOKIE_NAME: "detail-db-error"},
                )
    assert response.status_code == 503
    assert "Briefs temporarily unavailable" in response.text
    assert "Could not load this brief from the database." in response.text
    assert "Retry" in response.text
    assert "Return to the briefs list" in response.text
    assert "connection refused" not in response.text
    assert "postgresql://" not in response.text


@pytest.mark.unit
@pytest.mark.integration
def test_admin_brief_detail_malformed_id_returns_authenticated_admin_shell_404() -> None:
    token_hash = admin_auth.hash_session_token("detail-bad-id")
    row = _session_row(token_hash=token_hash)
    with mock_db_connection():
        with patch("app.admin_routes.db.get_admin_session_by_token_hash", return_value=row):
            with patch("app.admin_routes.brief_service.get_brief") as get_brief:
                response = client.get(
                    "/admin/briefs/not-a-number",
                    cookies={SESSION_COOKIE_NAME: "detail-bad-id"},
                )
    assert response.status_code == 404
    assert "Brief not found" in response.text
    assert "No project brief exists with ID #not-a-number" in response.text
    assert 'meta name="robots" content="noindex, nofollow"' in response.text
    assert "application/json" not in response.headers.get("content-type", "")
    get_brief.assert_not_called()


@pytest.mark.unit
@pytest.mark.integration
def test_admin_brief_detail_negative_id_returns_authenticated_404_without_db() -> None:
    token_hash = admin_auth.hash_session_token("detail-negative-id")
    row = _session_row(token_hash=token_hash)
    with mock_db_connection():
        with patch("app.admin_routes.db.get_admin_session_by_token_hash", return_value=row):
            with patch("app.admin_routes.brief_service.get_brief") as get_brief:
                response = client.get(
                    "/admin/briefs/-5",
                    cookies={SESSION_COOKIE_NAME: "detail-negative-id"},
                )
    assert response.status_code == 404
    assert "Brief not found" in response.text
    assert "No project brief exists with ID #-5" in response.text
    get_brief.assert_not_called()


@pytest.mark.unit
@pytest.mark.integration
def test_admin_brief_detail_oversized_id_returns_authenticated_404_without_db() -> None:
    token_hash = admin_auth.hash_session_token("detail-oversized-id")
    row = _session_row(token_hash=token_hash)
    with mock_db_connection():
        with patch("app.admin_routes.db.get_admin_session_by_token_hash", return_value=row):
            with patch("app.admin_routes.brief_service.get_brief") as get_brief:
                response = client.get(
                    "/admin/briefs/2147483648",
                    cookies={SESSION_COOKIE_NAME: "detail-oversized-id"},
                )
    assert response.status_code == 404
    assert "Brief not found" in response.text
    assert "No project brief exists with ID #2147483648" in response.text
    get_brief.assert_not_called()


@pytest.mark.unit
@pytest.mark.integration
def test_admin_brief_detail_zero_id_returns_authenticated_404() -> None:
    token_hash = admin_auth.hash_session_token("detail-zero-id")
    row = _session_row(token_hash=token_hash)
    with mock_db_connection():
        with patch("app.admin_routes.db.get_admin_session_by_token_hash", return_value=row):
            with patch(
                "app.admin_routes.brief_service.get_brief",
                return_value=None,
            ) as get_brief:
                response = client.get(
                    "/admin/briefs/0",
                    cookies={SESSION_COOKIE_NAME: "detail-zero-id"},
                )
    assert response.status_code == 404
    assert "Brief not found" in response.text
    get_brief.assert_not_called()


@pytest.mark.unit
@pytest.mark.integration
def test_admin_brief_detail_programming_error_is_not_swallowed() -> None:
    token_hash = admin_auth.hash_session_token("detail-bug")
    row = _session_row(token_hash=token_hash)
    with mock_db_connection():
        with patch("app.admin_routes.db.get_admin_session_by_token_hash", return_value=row):
            with patch(
                "app.admin_routes.brief_service.get_brief",
                side_effect=ValueError("programming bug"),
            ):
                with pytest.raises(ValueError, match="programming bug"):
                    client.get(
                        "/admin/briefs/42",
                        cookies={SESSION_COOKIE_NAME: "detail-bug"},
                    )


@pytest.mark.unit
@pytest.mark.integration
def test_admin_brief_detail_retry_after_database_error_succeeds() -> None:
    import psycopg

    token_hash = admin_auth.hash_session_token("detail-retry")
    row = _session_row(token_hash=token_hash)
    with mock_db_connection():
        with patch("app.admin_routes.db.get_admin_session_by_token_hash", return_value=row):
            with patch(
                "app.admin_routes.brief_service.get_brief",
                side_effect=[psycopg.OperationalError("timeout"), _detail_brief()],
            ) as get_brief:
                failed = client.get(
                    "/admin/briefs/42",
                    cookies={SESSION_COOKIE_NAME: "detail-retry"},
                )
                recovered = client.get(
                    "/admin/briefs/42",
                    cookies={SESSION_COOKIE_NAME: "detail-retry"},
                )
    assert failed.status_code == 503
    assert recovered.status_code == 200
    assert "Project brief #42" in recovered.text
    assert get_brief.call_count == 2
