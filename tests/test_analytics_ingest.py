"""Tests for first-party browser analytics ingestion (#114)."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Iterator
from unittest.mock import MagicMock, patch

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from app import analytics_ingest
from app.analytics_event_schema import ConsentState
from app.config import Settings, get_settings
from app.main import app

client = TestClient(app)

VALID_SESSION = "550e8400-e29b-41d4-a716-446655440000"
IDEMPOTENCY_A = "660e8400-e29b-41d4-a716-446655440001"
IDEMPOTENCY_B = "770e8400-e29b-41d4-a716-446655440002"
UTC = timezone.utc
NOW = datetime(2026, 7, 15, 12, 0, 0, tzinfo=UTC)


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
    analytics_ingest._fallback_buckets.clear()


def _settings() -> Settings:
    return get_settings()


def _ingest_kwargs(**overrides: object) -> dict[str, object]:
    defaults: dict[str, object] = {
        "raw_body": json.dumps(_valid_event()).encode(),
        "origin": "http://testserver",
        "referer": None,
        "dnt_header": None,
        "user_agent": "Mozilla/5.0",
        "source_key": "127.0.0.1",
        "conn": None,
    }
    defaults.update(overrides)
    return defaults


def _mock_conn(cursor: MagicMock) -> MagicMock:
    conn = MagicMock()
    cm = MagicMock()
    cm.__enter__.return_value = cursor
    cm.__exit__.return_value = None
    conn.cursor.return_value = cm
    return conn


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
def test_site_host_from_settings_strips_www(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BASE_URL", "https://www.saberistic.com")
    assert analytics_ingest.site_host_from_settings(get_settings()) == "saberistic.com"


@pytest.mark.unit
def test_is_same_origin_request_uses_referer_when_origin_missing() -> None:
    settings = _settings()
    assert analytics_ingest.is_same_origin_request(
        origin=None,
        referer="http://testserver/about",
        settings=settings,
    )


@pytest.mark.unit
def test_is_same_origin_request_rejects_foreign_referer() -> None:
    settings = _settings()
    assert not analytics_ingest.is_same_origin_request(
        origin=None,
        referer="https://evil.example/page",
        settings=settings,
    )


@pytest.mark.unit
def test_is_same_origin_request_allows_missing_origin_and_referer() -> None:
    settings = _settings()
    assert analytics_ingest.is_same_origin_request(
        origin=None,
        referer=None,
        settings=settings,
    )


@pytest.mark.unit
def test_is_do_not_track_false_when_unset() -> None:
    assert not analytics_ingest.is_do_not_track(None)
    assert not analytics_ingest.is_do_not_track("0")


@pytest.mark.unit
def test_is_bot_user_agent_false_for_normal_browser() -> None:
    assert not analytics_ingest.is_bot_user_agent(
        "Mozilla/5.0 (Macintosh; Intel Mac OS X) AppleWebKit/537.36"
    )
    assert not analytics_ingest.is_bot_user_agent(None)


@pytest.mark.unit
def test_parse_ingest_request_invalid_json() -> None:
    request, error = analytics_ingest.parse_ingest_request(b"{not-json")
    assert request is None
    assert error == "invalid_json"


@pytest.mark.unit
def test_parse_ingest_request_not_dict() -> None:
    request, error = analytics_ingest.parse_ingest_request(b"[1, 2]")
    assert request is None
    assert error == "invalid_json"


@pytest.mark.unit
def test_parse_ingest_request_validation_error() -> None:
    request, error = analytics_ingest.parse_ingest_request(
        json.dumps({"event_name": "Landing Viewed"}).encode()
    )
    assert request is None
    assert error == "validation"


@pytest.mark.unit
def test_parse_ingest_request_valid() -> None:
    request, error = analytics_ingest.parse_ingest_request(
        json.dumps(_valid_event()).encode()
    )
    assert error is None
    assert request is not None
    assert request.event_name == "Landing Viewed"


@pytest.mark.unit
def test_to_event_payload_maps_request() -> None:
    request, _ = analytics_ingest.parse_ingest_request(json.dumps(_valid_event()).encode())
    assert request is not None
    payload = analytics_ingest.to_event_payload(request)
    assert payload.event_name == "Landing Viewed"
    assert payload.anonymous_session_id == VALID_SESSION


@pytest.mark.unit
def test_ingest_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FIRST_PARTY_ANALYTICS_ENABLED", raising=False)
    result = analytics_ingest.ingest_browser_event(_settings(), **_ingest_kwargs())
    assert not result.accepted
    assert result.reason == analytics_ingest.IngestRejectReason.DISABLED


@pytest.mark.unit
def test_ingest_body_too_large() -> None:
    result = analytics_ingest.ingest_browser_event(
        _settings(),
        **_ingest_kwargs(raw_body=b"x" * 9000),
    )
    assert not result.accepted
    assert result.reason == analytics_ingest.IngestRejectReason.BODY_TOO_LARGE


@pytest.mark.unit
def test_ingest_invalid_json() -> None:
    result = analytics_ingest.ingest_browser_event(
        _settings(),
        **_ingest_kwargs(raw_body=b"{bad"),
    )
    assert not result.accepted
    assert result.reason == analytics_ingest.IngestRejectReason.INVALID_JSON


@pytest.mark.unit
def test_ingest_schema_version_mismatch() -> None:
    result = analytics_ingest.ingest_browser_event(
        _settings(),
        **_ingest_kwargs(
            raw_body=json.dumps(_valid_event(schema_version="9.9.9")).encode(),
        ),
    )
    assert not result.accepted
    assert result.reason == analytics_ingest.IngestRejectReason.VALIDATION


@pytest.mark.unit
def test_ingest_server_only_event_name() -> None:
    result = analytics_ingest.ingest_browser_event(
        _settings(),
        **_ingest_kwargs(
            raw_body=json.dumps(
                _valid_event(
                    event_name="Payment Completed",
                    properties={"brief_id": 1, "price_cents": 100, "funnel_step": 7},
                )
            ).encode(),
        ),
    )
    assert not result.accepted
    assert result.reason == analytics_ingest.IngestRejectReason.SERVER_EVENT


@pytest.mark.unit
def test_ingest_unknown_event_name() -> None:
    result = analytics_ingest.ingest_browser_event(
        _settings(),
        **_ingest_kwargs(
            raw_body=json.dumps(_valid_event(event_name="Mystery Event")).encode(),
        ),
    )
    assert not result.accepted
    assert result.reason == analytics_ingest.IngestRejectReason.VALIDATION


@pytest.mark.unit
def test_ingest_dnt_rejection() -> None:
    result = analytics_ingest.ingest_browser_event(
        _settings(),
        **_ingest_kwargs(dnt_header="1"),
    )
    assert not result.accepted
    assert result.reason == analytics_ingest.IngestRejectReason.CONSENT_DECLINED


@pytest.mark.unit
def test_ingest_origin_rejection() -> None:
    result = analytics_ingest.ingest_browser_event(
        _settings(),
        **_ingest_kwargs(origin="https://evil.example"),
    )
    assert not result.accepted
    assert result.reason == analytics_ingest.IngestRejectReason.ORIGIN


@pytest.mark.unit
def test_ingest_bot_rejection() -> None:
    result = analytics_ingest.ingest_browser_event(
        _settings(),
        **_ingest_kwargs(user_agent="curl/8.0"),
    )
    assert not result.accepted
    assert result.reason == analytics_ingest.IngestRejectReason.BOT


@pytest.mark.unit
def test_ingest_session_invalid_with_database() -> None:
    conn = _mock_conn(MagicMock())
    with patch.object(
        analytics_ingest,
        "touch_analytics_session",
        return_value=(False, True),
    ), patch.object(
        analytics_ingest,
        "try_admit_analytics_event",
        return_value=True,
    ):
        result = analytics_ingest.ingest_browser_event(
            _settings(),
            **_ingest_kwargs(conn=conn),
        )
    assert not result.accepted
    assert result.reason == analytics_ingest.IngestRejectReason.SESSION
    assert result.rotate_session


@pytest.mark.unit
def test_ingest_accepted_without_database() -> None:
    result = analytics_ingest.ingest_browser_event(_settings(), **_ingest_kwargs())
    assert result.accepted
    assert result.session_id == VALID_SESSION
    assert not result.duplicate


@pytest.mark.unit
def test_ingest_rate_limit_without_database() -> None:
    settings = _settings()
    admitted = 0
    for _ in range(settings.analytics_ingest_rate_limit + 2):
        result = analytics_ingest.ingest_browser_event(
            settings,
            **_ingest_kwargs(
                raw_body=json.dumps(
                    _valid_event(
                        idempotency_key="880e8400-e29b-41d4-a716-446655440003",
                    )
                ).encode(),
            ),
        )
        if result.accepted:
            admitted += 1
        else:
            assert result.reason == analytics_ingest.IngestRejectReason.RATE_LIMIT
            break
    assert admitted == settings.analytics_ingest_rate_limit


@pytest.mark.unit
def test_ingest_database_persistence_error() -> None:
    conn = _mock_conn(MagicMock())
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
        side_effect=RuntimeError("db down"),
    ):
        result = analytics_ingest.ingest_browser_event(
            _settings(),
            **_ingest_kwargs(conn=conn),
        )
    assert not result.accepted
    assert result.reason == analytics_ingest.IngestRejectReason.DATABASE


@pytest.mark.unit
def test_touch_analytics_session_invalid_id() -> None:
    conn = _mock_conn(MagicMock())
    valid, rotate = analytics_ingest.touch_analytics_session(
        conn,
        session_id="bad-id",
        now=NOW,
    )
    assert not valid
    assert rotate


@pytest.mark.unit
def test_touch_analytics_session_creates_new() -> None:
    cursor = MagicMock()
    cursor.fetchone.return_value = None
    conn = _mock_conn(cursor)
    valid, rotate = analytics_ingest.touch_analytics_session(
        conn,
        session_id=VALID_SESSION,
        now=NOW,
    )
    assert valid
    assert not rotate
    conn.commit.assert_called()


@pytest.mark.unit
def test_touch_analytics_session_expired() -> None:
    cursor = MagicMock()
    cursor.fetchone.return_value = {
        "created_at": NOW - timedelta(hours=1),
        "last_seen_at": NOW - timedelta(minutes=5),
        "expires_at": NOW - timedelta(seconds=1),
    }
    conn = _mock_conn(cursor)
    valid, rotate = analytics_ingest.touch_analytics_session(
        conn,
        session_id=VALID_SESSION,
        now=NOW,
    )
    assert not valid
    assert rotate


@pytest.mark.unit
def test_touch_analytics_session_max_age_exceeded() -> None:
    cursor = MagicMock()
    cursor.fetchone.return_value = {
        "created_at": NOW - timedelta(days=2),
        "last_seen_at": NOW - timedelta(hours=1),
        "expires_at": NOW + timedelta(hours=1),
    }
    conn = _mock_conn(cursor)
    valid, rotate = analytics_ingest.touch_analytics_session(
        conn,
        session_id=VALID_SESSION,
        now=NOW,
    )
    assert not valid
    assert rotate


@pytest.mark.unit
def test_touch_analytics_session_rotation() -> None:
    cursor = MagicMock()
    cursor.fetchone.return_value = {
        "created_at": NOW - timedelta(hours=1),
        "last_seen_at": NOW - timedelta(minutes=45),
        "expires_at": NOW + timedelta(hours=12),
    }
    conn = _mock_conn(cursor)
    valid, rotate = analytics_ingest.touch_analytics_session(
        conn,
        session_id=VALID_SESSION,
        now=NOW,
    )
    assert valid
    assert rotate


@pytest.mark.unit
def test_touch_analytics_session_updates_existing() -> None:
    cursor = MagicMock()
    cursor.fetchone.return_value = {
        "created_at": NOW - timedelta(minutes=10),
        "last_seen_at": NOW - timedelta(minutes=5),
        "expires_at": NOW + timedelta(hours=12),
    }
    conn = _mock_conn(cursor)
    valid, rotate = analytics_ingest.touch_analytics_session(
        conn,
        session_id=VALID_SESSION,
        now=NOW,
    )
    assert valid
    assert not rotate


@pytest.mark.unit
def test_persist_analytics_event_inserted() -> None:
    cursor = MagicMock()
    cursor.fetchone.return_value = {"id": 1}
    conn = _mock_conn(cursor)
    request, _ = analytics_ingest.parse_ingest_request(json.dumps(_valid_event()).encode())
    assert request is not None
    event = analytics_ingest.to_event_payload(request)
    inserted = analytics_ingest.persist_analytics_event(
        conn,
        idempotency_key=IDEMPOTENCY_A,
        event=event,
        received_at=NOW,
    )
    assert inserted
    conn.commit.assert_called()


@pytest.mark.unit
def test_persist_analytics_event_duplicate() -> None:
    cursor = MagicMock()
    cursor.fetchone.return_value = None
    conn = _mock_conn(cursor)
    request, _ = analytics_ingest.parse_ingest_request(json.dumps(_valid_event()).encode())
    assert request is not None
    event = analytics_ingest.to_event_payload(request)
    inserted = analytics_ingest.persist_analytics_event(
        conn,
        idempotency_key=IDEMPOTENCY_A,
        event=event,
        received_at=NOW,
    )
    assert not inserted


@pytest.mark.unit
def test_try_admit_analytics_event_fallback_window_reset() -> None:
    key = "analytics:session:test"
    past = NOW - timedelta(seconds=120)
    analytics_ingest._fallback_buckets[key] = (5, past)
    admitted = analytics_ingest._try_admit_fallback(
        key,
        limit=10,
        window_seconds=60,
        now=NOW,
    )
    assert admitted


@pytest.mark.unit
def test_try_admit_analytics_event_pg_locked() -> None:
    session_key = "analytics:session:abc"
    source_key = "analytics:source:127.0.0.1"
    locked_until = NOW + timedelta(minutes=5)
    cursor = MagicMock()
    cursor.fetchall.return_value = [
        {
            "limiter_key": session_key,
            "event_count": 60,
            "window_started_at": NOW,
            "locked_until": locked_until,
        },
        {
            "limiter_key": source_key,
            "event_count": 0,
            "window_started_at": NOW,
            "locked_until": None,
        },
    ]
    conn = _mock_conn(cursor)
    admitted = analytics_ingest.try_admit_analytics_event(
        conn,
        session_key="abc",
        source_key="127.0.0.1",
        now=NOW,
        rate_limit=60,
        window_seconds=60,
        lockout_seconds=300,
    )
    assert not admitted


@pytest.mark.unit
def test_try_admit_analytics_event_pg_admits() -> None:
    session_key = "analytics:session:abc"
    source_key = "analytics:source:127.0.0.1"
    cursor = MagicMock()
    cursor.fetchall.return_value = [
        {
            "limiter_key": session_key,
            "event_count": 1,
            "window_started_at": NOW,
            "locked_until": None,
        },
        {
            "limiter_key": source_key,
            "event_count": 1,
            "window_started_at": NOW,
            "locked_until": None,
        },
    ]
    conn = _mock_conn(cursor)
    admitted = analytics_ingest.try_admit_analytics_event(
        conn,
        session_key="abc",
        source_key="127.0.0.1",
        now=NOW,
        rate_limit=60,
        window_seconds=60,
        lockout_seconds=300,
    )
    assert admitted


@pytest.mark.unit
def test_try_admit_analytics_event_pg_window_reset() -> None:
    session_key = "analytics:session:abc"
    source_key = "analytics:source:127.0.0.1"
    cursor = MagicMock()
    cursor.fetchall.return_value = [
        {
            "limiter_key": session_key,
            "event_count": 99,
            "window_started_at": NOW - timedelta(minutes=5),
            "locked_until": None,
        },
        {
            "limiter_key": source_key,
            "event_count": 99,
            "window_started_at": NOW - timedelta(minutes=5),
            "locked_until": None,
        },
    ]
    conn = _mock_conn(cursor)
    admitted = analytics_ingest.try_admit_analytics_event(
        conn,
        session_key="abc",
        source_key="127.0.0.1",
        now=NOW,
        rate_limit=60,
        window_seconds=60,
        lockout_seconds=300,
    )
    assert admitted


@pytest.mark.unit
def test_try_admit_analytics_event_pg_exceeds_limit() -> None:
    session_key = "analytics:session:abc"
    source_key = "analytics:source:127.0.0.1"
    cursor = MagicMock()
    cursor.fetchall.return_value = [
        {
            "limiter_key": session_key,
            "event_count": 60,
            "window_started_at": NOW,
            "locked_until": None,
        },
        {
            "limiter_key": source_key,
            "event_count": 0,
            "window_started_at": NOW,
            "locked_until": None,
        },
    ]
    conn = _mock_conn(cursor)
    admitted = analytics_ingest.try_admit_analytics_event(
        conn,
        session_key="abc",
        source_key="127.0.0.1",
        now=NOW,
        rate_limit=60,
        window_seconds=60,
        lockout_seconds=300,
    )
    assert admitted
    update_calls = [
        call.args[1]
        for call in cursor.execute.call_args_list
        if call.args and "UPDATE analytics_event_rate_limits" in call.args[0]
    ]
    assert update_calls
    locked_values = [args[2] for args in update_calls]
    assert NOW + timedelta(seconds=300) in locked_values


@pytest.mark.unit
def test_first_party_analytics_js_exists_and_documents_contract() -> None:
    response = client.get("/assets/first_party_analytics.js")
    assert response.status_code == 200
    body = response.text
    assert "sendBeacon" in body
    assert "keepalive" in body
    assert "navigator.doNotTrack" in body
    assert "globalPrivacyControl" in body
    assert "/api/events" in body
    assert "saberistic-first-party-page-event" in body


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
def test_ingest_sets_session_rotate_header(same_origin_headers: dict[str, str]) -> None:
    with mock_ingest_conn():
        with patch.object(
            analytics_ingest,
            "touch_analytics_session",
            return_value=(True, True),
        ):
            response = client.post("/api/events", json=_valid_event(), headers=same_origin_headers)
    assert response.status_code == 200
    assert response.headers.get("x-analytics-session-rotate") == "1"


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
    conn = psycopg.connect(database_url, row_factory=dict_row, autocommit=False)
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
        cur.execute("SELECT COUNT(*) AS n FROM analytics_events")
        row = cur.fetchone()
        assert row is not None
        count = row["n"]
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
