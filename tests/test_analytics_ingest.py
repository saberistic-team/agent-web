"""Tests for first-party browser analytics ingestion (#114)."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator
from unittest.mock import MagicMock, patch

import psycopg
import pytest
from fastapi.testclient import TestClient

from app import analytics_ingest
from app.analytics_event_schema import ConsentState
from app.config import get_settings
from app.main import app

client = TestClient(app)

VALID_SESSION = "550e8400-e29b-41d4-a716-446655440000"
IDEMPOTENCY_A = "660e8400-e29b-41d4-a716-446655440001"
IDEMPOTENCY_B = "770e8400-e29b-41d4-a716-446655440002"
UTC = timezone.utc


def _valid_event(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "idempotency_key": IDEMPOTENCY_A,
        "event_name": "Landing Viewed",
        "schema_version": "1.0.0",
        "occurred_at": "2026-07-15T12:00:00+00:00",
        "anonymous_session_id": VALID_SESSION,
        "path_class": "landing",
        "referrer_class": "direct",
        "attribution": {"utm_source": "linkedin"},
        "properties": {"page": "/", "funnel_step": 1},
        "consent_state": ConsentState.IMPLICIT_ANALYTICS.value,
    }
    body.update(overrides)
    return body


@pytest.fixture(autouse=True)
def ingest_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("FIRST_PARTY_ANALYTICS_ENABLED", "true")
    monkeypatch.delenv("ANALYTICS_ENABLED", raising=False)


@pytest.fixture
def same_origin_headers() -> dict[str, str]:
    return {
        "Origin": "http://testserver",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X) AppleWebKit/537.36",
    }


@contextmanager
def mock_ingest_conn(*, inserted: bool = True) -> Iterator[MagicMock]:
    conn = MagicMock()
    with patch("app.main.db.db_connection") as db_conn:
        db_conn.return_value.__enter__.return_value = conn
        db_conn.return_value.__exit__.return_value = None
        with patch.object(
            analytics_ingest,
            "touch_analytics_session",
            return_value=(True, False),
        ), patch.object(
            analytics_ingest,
            "try_admit_analytics_event",
            return_value=True,
        ), patch.object(
            analytics_ingest,
            "persist_analytics_event",
            return_value=inserted,
        ):
            yield conn


@pytest.mark.unit
def test_first_party_analytics_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FIRST_PARTY_ANALYTICS_ENABLED", raising=False)
    settings = get_settings()
    assert not settings.first_party_analytics_enabled


@pytest.mark.unit
def test_is_same_origin_request_accepts_matching_origin() -> None:
    settings = get_settings()
    assert analytics_ingest.is_same_origin_request(
        origin="http://testserver",
        referer=None,
        settings=settings,
    )


@pytest.mark.unit
def test_is_same_origin_request_rejects_foreign_origin() -> None:
    settings = get_settings()
    assert not analytics_ingest.is_same_origin_request(
        origin="https://evil.example",
        referer=None,
        settings=settings,
    )


@pytest.mark.unit
def test_is_bot_user_agent_detects_crawler() -> None:
    assert analytics_ingest.is_bot_user_agent("Googlebot/2.1")


@pytest.mark.unit
def test_parse_ingest_request_rejects_oversized_body() -> None:
    request, error = analytics_ingest.parse_ingest_request(b"x" * 9000)
    assert request is None
    assert error == "body_too_large"


@pytest.mark.unit
def test_ingest_rejects_sensitive_property_without_logging_value(
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = get_settings()
    body = _valid_event(
        properties={"page": "/", "funnel_step": 1, "email": "secret@example.com"},
    )
    with caplog.at_level("INFO"):
        result = analytics_ingest.ingest_browser_event(
            settings,
            raw_body=json.dumps(body).encode(),
            origin="http://testserver",
            referer=None,
            dnt_header=None,
            user_agent="Mozilla/5.0",
            source_key="127.0.0.1",
            conn=None,
        )
    assert not result.accepted
    assert result.reason == analytics_ingest.IngestRejectReason.VALIDATION
    assert "secret@example.com" not in caplog.text


@pytest.mark.integration
def test_ingest_endpoint_disabled_returns_404(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FIRST_PARTY_ANALYTICS_ENABLED", raising=False)
    response = client.post("/api/events", json=_valid_event())
    assert response.status_code == 404


@pytest.mark.integration
def test_ingest_valid_event_accepted(same_origin_headers: dict[str, str]) -> None:
    with mock_ingest_conn():
        response = client.post("/api/events", json=_valid_event(), headers=same_origin_headers)
    assert response.status_code == 200
    assert response.json()["accepted"] is True
    assert response.cookies.get("saber_analytics_sid") == VALID_SESSION


@pytest.mark.integration
def test_ingest_duplicate_idempotency_key(same_origin_headers: dict[str, str]) -> None:
    with mock_ingest_conn(inserted=False):
        response = client.post("/api/events", json=_valid_event(), headers=same_origin_headers)
    assert response.status_code == 200
    assert response.json()["duplicate"] is True


@pytest.mark.integration
def test_ingest_rejects_cross_origin(same_origin_headers: dict[str, str]) -> None:
    headers = dict(same_origin_headers)
    headers["Origin"] = "https://evil.example"
    with mock_ingest_conn():
        response = client.post("/api/events", json=_valid_event(), headers=headers)
    assert response.status_code == 400
    assert response.json()["detail"] == "origin"


@pytest.mark.integration
def test_ingest_rejects_dnt_header(same_origin_headers: dict[str, str]) -> None:
    headers = dict(same_origin_headers)
    headers["DNT"] = "1"
    with mock_ingest_conn():
        response = client.post("/api/events", json=_valid_event(), headers=headers)
    assert response.status_code == 400
    assert response.json()["detail"] == "consent_declined"


@pytest.mark.integration
def test_ingest_rejects_declined_consent(same_origin_headers: dict[str, str]) -> None:
    with mock_ingest_conn():
        response = client.post(
            "/api/events",
            json=_valid_event(consent_state=ConsentState.DECLINED.value),
            headers=same_origin_headers,
        )
    assert response.status_code == 400
    assert response.json()["detail"] == "consent_declined"


@pytest.mark.integration
def test_ingest_rejects_server_only_event(same_origin_headers: dict[str, str]) -> None:
    with mock_ingest_conn():
        response = client.post(
            "/api/events",
            json=_valid_event(
                event_name="Lead Persisted",
                properties={"brief_id": 1, "funnel_step": 5},
            ),
            headers=same_origin_headers,
        )
    assert response.status_code == 400
    assert response.json()["detail"] == "server_event"


@pytest.mark.integration
def test_ingest_rejects_bot_user_agent(same_origin_headers: dict[str, str]) -> None:
    headers = dict(same_origin_headers)
    headers["User-Agent"] = "Googlebot/2.1 (+http://www.google.com/bot.html)"
    with mock_ingest_conn():
        response = client.post("/api/events", json=_valid_event(), headers=headers)
    assert response.status_code == 400
    assert response.json()["detail"] == "bot"


@pytest.mark.integration
def test_ingest_rate_limit_returns_429(same_origin_headers: dict[str, str]) -> None:
    with mock_ingest_conn():
        with patch.object(
            analytics_ingest,
            "try_admit_analytics_event",
            return_value=False,
        ):
            response = client.post("/api/events", json=_valid_event(), headers=same_origin_headers)
    assert response.status_code == 429
    assert response.json()["detail"] == "rate_limit"


@pytest.mark.integration
def test_first_party_analytics_script_injected_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FIRST_PARTY_ANALYTICS_ENABLED", "true")
    response = client.get("/")
    assert response.status_code == 200
    assert 'name="saberistic-first-party-analytics"' in response.text
    assert 'src="/assets/first_party_analytics.js"' in response.text


@pytest.mark.integration
def test_site_works_when_first_party_analytics_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FIRST_PARTY_ANALYTICS_ENABLED", raising=False)
    response = client.get("/")
    assert response.status_code == 200
    assert "saberistic-first-party-analytics" not in response.text


_REQUIRED = (os.environ.get("REQUIRE_TEST_DATABASE") or "").strip() in {"1", "true", "yes"}
_DATABASE_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip()


def _require_database_url() -> str:
    if _DATABASE_URL:
        return _DATABASE_URL
    if _REQUIRED:
        pytest.fail("REQUIRE_TEST_DATABASE=1 but TEST_DATABASE_URL is unset")
    pytest.skip("TEST_DATABASE_URL not set; skipping live Postgres analytics ingest tests")


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
    conn = psycopg.connect(database_url, autocommit=False)
    try:
        yield conn
    finally:
        conn.close()
        with psycopg.connect(database_url, autocommit=False) as cleanup:
            _reset_public_schema(cleanup)


@pytest.mark.integration
def test_persist_and_deduplicate_event_pg(pg_conn: psycopg.Connection) -> None:
    settings = get_settings()
    body = _valid_event()
    raw = json.dumps(body).encode()

    first = analytics_ingest.ingest_browser_event(
        settings,
        raw_body=raw,
        origin="http://testserver",
        referer=None,
        dnt_header=None,
        user_agent="Mozilla/5.0",
        source_key="127.0.0.1",
        conn=pg_conn,
    )
    assert first.accepted
    assert not first.duplicate

    second = analytics_ingest.ingest_browser_event(
        settings,
        raw_body=raw,
        origin="http://testserver",
        referer=None,
        dnt_header=None,
        user_agent="Mozilla/5.0",
        source_key="127.0.0.1",
        conn=pg_conn,
    )
    assert second.accepted
    assert second.duplicate

    retry = analytics_ingest.ingest_browser_event(
        settings,
        raw_body=json.dumps(_valid_event(idempotency_key=IDEMPOTENCY_B)).encode(),
        origin="http://testserver",
        referer=None,
        dnt_header=None,
        user_agent="Mozilla/5.0",
        source_key="127.0.0.1",
        conn=pg_conn,
    )
    assert retry.accepted
    assert not retry.duplicate

    with pg_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM analytics_events")
        count = cur.fetchone()[0]
    assert count == 2


@pytest.mark.integration
def test_rate_limit_pg(pg_conn: psycopg.Connection) -> None:
    settings = get_settings()
    admitted = 0
    for index in range(65):
        body = _valid_event(
            idempotency_key=f"{index:08x}-0000-4000-8000-000000000000",
        )
        result = analytics_ingest.ingest_browser_event(
            settings,
            raw_body=json.dumps(body).encode(),
            origin="http://testserver",
            referer=None,
            dnt_header=None,
            user_agent="Mozilla/5.0",
            source_key="test-rate-limit",
            conn=pg_conn,
        )
        if result.accepted:
            admitted += 1
        else:
            assert result.reason == analytics_ingest.IngestRejectReason.RATE_LIMIT
            break
    assert admitted <= settings.analytics_ingest_rate_limit
