"""Tests for the read-only admin briefs list (#146)."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Any, Generator
from unittest.mock import MagicMock, patch

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app import admin_auth, brief_service
from app.admin_auth import SESSION_COOKIE_NAME
from app.admin_pages import render_admin_briefs_page
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


def _sample_brief() -> dict[str, Any]:
    return {
        "id": 42,
        "created_at": datetime(2026, 7, 14, 10, 30, tzinfo=timezone.utc),
        "website": "https://acme.example",
        "contact_value": "ops@acme.example",
        "status": "paid",
        "paid_at": datetime(2026, 7, 14, 10, 45, tzinfo=timezone.utc),
        "amount_subtotal_cents": 20_000,
        "amount_discount_cents": 0,
        "amount_total_cents": 20_000,
        "currency": "usd",
        "utm_source": "linkedin",
        "utm_campaign": "spring-launch",
    }


@pytest.mark.unit
def test_normalize_filters_clamps_page_and_bounds_query() -> None:
    filters = brief_service.normalize_filters(
        page=0,
        per_page=500,
        query="  find-me  ",
        status="paid",
        date_from="2026-07-01",
        date_to="2026-06-01",
    )
    assert filters.page == 1
    assert filters.per_page == 100
    assert filters.query == "find-me"
    assert filters.status == "paid"
    assert filters.date_from == date(2026, 6, 1)
    assert filters.date_to == date(2026, 7, 1)


@pytest.mark.unit
def test_normalize_filters_ignores_invalid_status() -> None:
    filters = brief_service.normalize_filters(status="refunded")
    assert filters.status is None


@pytest.mark.unit
def test_list_briefs_delegates_to_repository() -> None:
    conn = MagicMock()
    repo = MagicMock()
    repo.list_page.return_value = ([{"id": 1}], 1)
    rows, total, filters = brief_service.list_briefs(
        conn,
        page=2,
        per_page=25,
        query="acme",
        status="paid",
        date_from="2026-07-01",
        date_to="2026-07-14",
        repository=repo,
    )
    assert total == 1
    assert rows[0]["id"] == 1
    assert filters.page == 2
    assert filters.per_page == 25
    repo.list_page.assert_called_once_with(
        conn,
        page=2,
        per_page=25,
        query="acme",
        status="paid",
        date_from=date(2026, 7, 1),
        date_to=date(2026, 7, 14),
    )


@pytest.mark.unit
def test_postgres_project_brief_repository_list_page_orders_newest_first() -> None:
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    cursor.fetchone.return_value = {"total": 2}
    cursor.fetchall.return_value = [{"id": 9}, {"id": 8}]
    repo = PostgresProjectBriefRepository()
    rows, total = repo.list_page(conn, page=1, per_page=10, query="ops@acme.example")
    assert total == 2
    assert len(rows) == 2
    count_sql = cursor.execute.call_args_list[0][0][0]
    list_sql = cursor.execute.call_args_list[1][0][0]
    assert "COUNT(*)" in count_sql
    assert "contact_value ILIKE" in count_sql
    assert "ORDER BY created_at DESC, id DESC" in list_sql
    assert " brief" not in list_sql.lower()


@pytest.mark.unit
def test_postgres_project_brief_repository_numeric_query_matches_id() -> None:
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    cursor.fetchone.return_value = {"total": 1}
    cursor.fetchall.return_value = [{"id": 7}]
    repo = PostgresProjectBriefRepository()
    repo.list_page(conn, page=1, per_page=10, query="7")
    count_sql = cursor.execute.call_args_list[0][0][0]
    assert "id = %s" in count_sql


@pytest.mark.unit
def test_postgres_project_brief_repository_applies_status_and_dates() -> None:
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    cursor.fetchone.return_value = {"total": 0}
    cursor.fetchall.return_value = []
    repo = PostgresProjectBriefRepository()
    repo.list_page(
        conn,
        page=1,
        per_page=10,
        status="pending_payment",
        date_from=date(2026, 7, 1),
        date_to=date(2026, 7, 14),
    )
    count_sql = cursor.execute.call_args_list[0][0][0]
    assert "status = %s" in count_sql
    assert "created_at >=" in count_sql
    assert "created_at <" in count_sql


@pytest.mark.unit
def test_render_admin_briefs_page_escapes_html() -> None:
    filters = BriefListFilters(
        page=1,
        per_page=50,
        query=None,
        status=None,
        date_from=None,
        date_to=None,
        date_from_raw=None,
        date_to_raw=None,
    )
    html_out = render_admin_briefs_page(
        admin_username=TEST_USERNAME,
        briefs=[
            {
                "id": 1,
                "created_at": datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc),
                "website": '"><script>alert(1)</script>',
                "contact_value": "evil@example.com",
                "status": "pending_payment",
                "paid_at": None,
                "utm_source": None,
                "utm_campaign": None,
            }
        ],
        filters=filters,
        total=1,
        price_cents=20_000,
    )
    assert "<script>" not in html_out
    assert "&lt;script&gt;" in html_out or "&quot;&gt;&lt;script" in html_out
    assert 'meta name="robots" content="noindex, nofollow"' in html_out
    assert 'href="/admin/briefs/1"' in html_out


@pytest.mark.unit
def test_render_admin_briefs_page_detail_link_preserves_filters() -> None:
    filters = BriefListFilters(
        page=2,
        per_page=50,
        query="acme",
        status="paid",
        date_from=None,
        date_to=None,
        date_from_raw="2026-07-01",
        date_to_raw="2026-07-14",
    )
    html_out = render_admin_briefs_page(
        admin_username=TEST_USERNAME,
        briefs=[_sample_brief()],
        filters=filters,
        total=120,
        price_cents=20_000,
    )
    assert "/admin/briefs/42?page=2" in html_out
    assert "q=acme" in html_out
    assert "status=paid" in html_out


@pytest.mark.unit
def test_render_admin_briefs_page_empty_and_filtered_states() -> None:
    filters = BriefListFilters(
        page=1,
        per_page=50,
        query="missing",
        status=None,
        date_from=None,
        date_to=None,
        date_from_raw=None,
        date_to_raw=None,
    )
    empty = render_admin_briefs_page(
        admin_username=TEST_USERNAME,
        briefs=[],
        filters=BriefListFilters(
            page=1,
            per_page=50,
            query=None,
            status=None,
            date_from=None,
            date_to=None,
            date_from_raw=None,
            date_to_raw=None,
        ),
        total=0,
        price_cents=20_000,
    )
    assert "No project briefs submitted yet." in empty

    filtered = render_admin_briefs_page(
        admin_username=TEST_USERNAME,
        briefs=[],
        filters=filters,
        total=0,
        price_cents=20_000,
    )
    assert "No briefs match your filters." in filtered


@pytest.mark.unit
def test_render_admin_briefs_page_database_error_state() -> None:
    filters = BriefListFilters(
        page=1,
        per_page=50,
        query=None,
        status=None,
        date_from=None,
        date_to=None,
        date_from_raw=None,
        date_to_raw=None,
    )
    html_out = render_admin_briefs_page(
        admin_username=TEST_USERNAME,
        briefs=[],
        filters=filters,
        total=0,
        price_cents=20_000,
        db_error=True,
    )
    assert "Could not load briefs from the database." in html_out
    assert "temporarily unavailable" in html_out
    assert "SELECT" not in html_out


@pytest.mark.unit
def test_render_admin_briefs_page_preserves_filter_params_in_pager() -> None:
    filters = BriefListFilters(
        page=2,
        per_page=50,
        query="acme",
        status="paid",
        date_from=date(2026, 7, 1),
        date_to=date(2026, 7, 14),
        date_from_raw="2026-07-01",
        date_to_raw="2026-07-14",
    )
    html_out = render_admin_briefs_page(
        admin_username=TEST_USERNAME,
        briefs=[_sample_brief()],
        filters=filters,
        total=120,
        price_cents=20_000,
    )
    assert "Previous</a>" in html_out
    assert "page=3" in html_out
    assert "q=acme" in html_out
    assert "status=paid" in html_out
    assert "date_from=2026-07-01" in html_out
    assert "date_to=2026-07-14" in html_out
    assert "$200" in html_out
    assert "linkedin / spring-launch" in html_out


@pytest.mark.unit
def test_render_admin_briefs_page_shows_discounted_collected_amount() -> None:
    filters = BriefListFilters(
        page=1,
        per_page=50,
        query=None,
        status=None,
        date_from=None,
        date_to=None,
        date_from_raw=None,
        date_to_raw=None,
    )
    brief = _sample_brief()
    brief["amount_discount_cents"] = 10_000
    brief["amount_total_cents"] = 10_000
    html_out = render_admin_briefs_page(
        admin_username=TEST_USERNAME,
        briefs=[brief],
        filters=filters,
        total=1,
        price_cents=20_000,
    )
    assert "$100 (discounted)" in html_out
    assert "$200" not in html_out.split("Payment")[1]


@pytest.mark.unit
@pytest.mark.integration
def test_admin_briefs_page_requires_auth() -> None:
    response = client.get("/admin/briefs")
    assert response.status_code == 303
    assert "/admin/login" in response.headers["location"]


@pytest.mark.unit
@pytest.mark.integration
def test_admin_briefs_page_renders_rows() -> None:
    token_hash = admin_auth.hash_session_token("briefs-session")
    row = _session_row(token_hash=token_hash)
    briefs = [_sample_brief()]
    filters = BriefListFilters(
        page=1,
        per_page=50,
        query=None,
        status=None,
        date_from=None,
        date_to=None,
        date_from_raw=None,
        date_to_raw=None,
    )
    with mock_db_connection() as conn:
        with patch("app.admin_routes.db.get_admin_session_by_token_hash", return_value=row):
            with patch(
                "app.admin_routes.brief_service.list_briefs",
                return_value=(briefs, 1, filters),
            ) as list_briefs:
                response = client.get(
                    "/admin/briefs",
                    cookies={SESSION_COOKIE_NAME: "briefs-session"},
                )
    assert response.status_code == 200
    body = response.text
    assert "Submitted briefs" in body
    assert "ops@acme.example" in body
    assert "https://acme.example" in body
    assert 'href="/admin/briefs/42"' in body
    assert "super-secret project description" not in body
    list_briefs.assert_called_once()


@pytest.mark.unit
@pytest.mark.integration
def test_admin_briefs_page_passes_search_and_filters() -> None:
    token_hash = admin_auth.hash_session_token("briefs-filter-session")
    row = _session_row(token_hash=token_hash)
    filters = BriefListFilters(
        page=1,
        per_page=50,
        query="acme",
        status="paid",
        date_from=date(2026, 7, 1),
        date_to=date(2026, 7, 14),
        date_from_raw="2026-07-01",
        date_to_raw="2026-07-14",
    )
    with mock_db_connection():
        with patch("app.admin_routes.db.get_admin_session_by_token_hash", return_value=row):
            with patch(
                "app.admin_routes.brief_service.list_briefs",
                return_value=([], 0, filters),
            ) as list_briefs:
                response = client.get(
                    "/admin/briefs?q=acme&status=paid&date_from=2026-07-01&date_to=2026-07-14",
                    cookies={SESSION_COOKIE_NAME: "briefs-filter-session"},
                )
    assert response.status_code == 200
    list_briefs.assert_called_once()
    kwargs = list_briefs.call_args.kwargs
    assert kwargs["query"] == "acme"
    assert kwargs["status"] == "paid"
    assert kwargs["date_from"] == "2026-07-01"
    assert kwargs["date_to"] == "2026-07-14"


@pytest.mark.unit
@pytest.mark.integration
def test_admin_briefs_page_handles_database_errors() -> None:
    import psycopg

    token_hash = admin_auth.hash_session_token("briefs-db-error")
    row = _session_row(token_hash=token_hash)
    with mock_db_connection():
        with patch("app.admin_routes.db.get_admin_session_by_token_hash", return_value=row):
            with patch(
                "app.admin_routes.brief_service.list_briefs",
                side_effect=psycopg.OperationalError("connection refused"),
            ):
                response = client.get(
                    "/admin/briefs",
                    cookies={SESSION_COOKIE_NAME: "briefs-db-error"},
                )
    assert response.status_code == 200
    assert "Could not load briefs from the database." in response.text
    assert "connection refused" not in response.text
