"""Unit tests for shared admin login rate-limit database helpers."""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import psycopg
import pytest

from app import admin_auth, db

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://testuser:testpass@localhost:5432/agent_web_test",
)


def _postgres_available() -> bool:
    try:
        with psycopg.connect(TEST_DATABASE_URL, connect_timeout=2):
            return True
    except Exception:
        return False


postgres_required = pytest.mark.skipif(
    not _postgres_available(),
    reason="PostgreSQL integration database is unavailable",
)


@pytest.fixture(autouse=True)
def _reset_fallback_limiter() -> None:
    admin_auth.reset_login_rate_limiter()


@pytest.mark.unit
def test_admit_admin_login_attempt_allows_empty_key_list() -> None:
    conn = MagicMock()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    admitted, transition = db.admit_admin_login_attempt(
        conn,
        limiter_keys=[],
        now=now,
        rate_limit=5,
        window_seconds=900,
        lockout_seconds=900,
    )
    assert admitted
    assert not transition


@pytest.mark.unit
def test_is_admin_login_locked_false_for_empty_key_list() -> None:
    conn = MagicMock()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert not db.is_admin_login_locked(conn, limiter_keys=[], now=now)


@pytest.mark.unit
def test_is_admin_login_throttled_false_when_no_row() -> None:
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cur.fetchall.return_value = []

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert not db.is_admin_login_throttled(conn, limiter_key="abc", now=now)


@pytest.mark.unit
def test_is_admin_login_locked_true_when_any_bucket_locked() -> None:
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    cur.fetchall.return_value = [
        {"locked_until": datetime(2026, 1, 1, 13, 0, tzinfo=timezone.utc)},
    ]

    assert db.is_admin_login_locked(conn, limiter_keys=["abc", "def"], now=now)


@pytest.mark.unit
def test_is_admin_login_throttled_false_when_lockout_expired() -> None:
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    now = datetime(2026, 1, 1, 14, 0, tzinfo=timezone.utc)
    cur.fetchall.return_value = [
        {"locked_until": datetime(2026, 1, 1, 13, 0, tzinfo=timezone.utc)},
    ]

    assert not db.is_admin_login_locked(conn, limiter_keys=["abc"], now=now)


@pytest.mark.unit
def test_record_admin_login_failure_delegates_to_plural_helper() -> None:
    conn = MagicMock()
    with pytest.MonkeyPatch.context() as patcher:
        record = MagicMock()
        patcher.setattr(db, "record_admin_login_failures", record)
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        db.record_admin_login_failure(
            conn,
            limiter_key="key123",
            now=now,
            rate_limit=5,
            window_seconds=900,
            lockout_seconds=900,
        )
    record.assert_called_once_with(
        conn,
        limiter_keys=["key123"],
        now=now,
        rate_limit=5,
        window_seconds=900,
        lockout_seconds=900,
    )


@pytest.mark.unit
def test_clear_admin_login_rate_limit_deletes_row() -> None:
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur

    db.clear_admin_login_rate_limit(conn, limiter_key="key123")

    sql = cur.execute.call_args.args[0]
    assert "DELETE FROM admin_login_rate_limits" in sql
    assert cur.execute.call_args.args[1] == (["key123"],)
    conn.commit.assert_called_once()


@pytest.mark.unit
def test_cleanup_expired_admin_login_rate_limits_deletes_stale_rows() -> None:
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cur.rowcount = 3
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    deleted = db.cleanup_expired_admin_login_rate_limits(
        conn,
        now=now,
        window_seconds=900,
        lockout_seconds=900,
    )

    sql = cur.execute.call_args.args[0]
    assert "DELETE FROM admin_login_rate_limits" in sql
    assert deleted == 3
    conn.commit.assert_called_once()


@pytest.mark.unit
def test_login_inputs_within_limits_rejects_oversized_values() -> None:
    assert admin_auth.login_inputs_within_limits(
        username="a",
        password="b",
        csrf_token="c",
        flow_token="d",
    )
    assert not admin_auth.login_inputs_within_limits(
        username="x" * (admin_auth.LOGIN_USERNAME_MAX_LENGTH + 1),
        password="b",
        csrf_token="c",
    )
    assert not admin_auth.login_inputs_within_limits(
        username="a",
        password="x" * (admin_auth.LOGIN_PASSWORD_MAX_LENGTH + 1),
        csrf_token="c",
    )
    assert not admin_auth.login_inputs_within_limits(
        username="a",
        password="b",
        csrf_token="x" * (admin_auth.LOGIN_CSRF_MAX_LENGTH + 1),
    )
    assert not admin_auth.login_inputs_within_limits(
        username="a",
        password="b",
        csrf_token="c",
        flow_token="x" * (admin_auth.LOGIN_FLOW_TOKEN_MAX_LENGTH + 1),
    )


@pytest.mark.unit
def test_resolve_login_limiter_keys_omits_account_for_unknown_username(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_USERNAME", "operator")
    from app.config import get_settings

    settings = get_settings()
    keys = admin_auth.resolve_login_limiter_keys(
        submitted_username="ghost",
        client_source="203.0.113.1",
        settings=settings,
    )
    assert len(keys) == 1
    assert keys[0] == admin_auth.build_source_limiter_key("203.0.113.1")


@pytest.mark.unit
def test_record_failed_login_uses_fallback_when_database_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    admin_auth.reset_login_rate_limiter()
    request = MagicMock()
    request.headers = {}
    request.client = MagicMock(host="203.0.113.5")
    from app.config import get_settings

    settings = get_settings()
    with patch("app.admin_auth.db.db_connection", side_effect=RuntimeError("db down")):
        admin_auth.record_failed_login(
            request,
            settings,
            username="operator",
        )
    keys = admin_auth.resolve_login_limiter_keys(
        submitted_username="operator",
        client_source="203.0.113.5",
        settings=settings,
    )
    assert all(key in admin_auth._fallback_attempts for key in keys)


@pytest.mark.unit
def test_admit_login_verification_uses_fallback_when_database_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    admin_auth.reset_login_rate_limiter()
    request = MagicMock()
    request.headers = {}
    request.client = MagicMock(host="203.0.113.5")
    from app.config import get_settings

    settings = get_settings()
    with patch("app.admin_auth.db.db_connection", side_effect=RuntimeError("db down")):
        outcome = admin_auth.admit_login_verification(
            request,
            settings,
            username="operator",
        )
    assert outcome.admitted
    assert outcome.store_unavailable is False


@pytest.mark.unit
def test_is_login_locked_uses_fallback_when_database_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    admin_auth.reset_login_rate_limiter()
    request = MagicMock()
    request.headers = {}
    request.client = MagicMock(host="203.0.113.5")
    from app.config import get_settings

    settings = get_settings()
    keys = admin_auth.resolve_login_limiter_keys(
        submitted_username="operator",
        client_source="203.0.113.5",
        settings=settings,
    )
    for key in keys:
        admin_auth._fallback_attempts[key] = (2, time.time())
    with patch("app.admin_auth.db.db_connection", side_effect=RuntimeError("db down")):
        assert admin_auth.is_login_locked(request, settings, username="operator")


@pytest.mark.unit
def test_clear_login_rate_limit_uses_fallback_when_database_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", "operator")
    admin_auth.reset_login_rate_limiter()
    request = MagicMock()
    request.headers = {}
    request.client = MagicMock(host="203.0.113.5")
    from app.config import get_settings

    settings = get_settings()
    account_key = admin_auth.build_account_limiter_key(settings)
    admin_auth._fallback_attempts[account_key] = (1, 0.0)
    with patch("app.admin_auth.db.db_connection", side_effect=RuntimeError("db down")):
        admin_auth.clear_login_rate_limit(request, settings)
    assert account_key not in admin_auth._fallback_attempts


@postgres_required
@pytest.mark.unit
def test_admit_and_release_admin_login_admission_round_trip() -> None:
    db.init_db(TEST_DATABASE_URL)
    now = datetime(2026, 2, 1, tzinfo=timezone.utc)
    key = admin_auth.build_source_limiter_key("203.0.113.200")
    with db.db_connection(TEST_DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM admin_login_rate_limits WHERE limiter_key = %s", (key,))
            conn.commit()
        admitted, _ = db.admit_admin_login_attempt(
            conn,
            limiter_keys=[key],
            now=now,
            rate_limit=5,
            window_seconds=900,
            lockout_seconds=900,
        )
        assert admitted
        db.release_admin_login_admission(
            conn,
            limiter_key=key,
            now=now,
            rate_limit=5,
        )
        with conn.cursor() as cur:
            cur.execute(
                "SELECT failure_count, locked_until FROM admin_login_rate_limits WHERE limiter_key = %s",
                (key,),
            )
            row = cur.fetchone()
    assert row is not None
    assert row["failure_count"] == 0
    assert row["locked_until"] is None



@postgres_required
@pytest.mark.unit
def test_release_admin_login_admission_noop_when_row_missing() -> None:
    db.init_db(TEST_DATABASE_URL)
    now = datetime(2026, 2, 1, tzinfo=timezone.utc)
    key = admin_auth.build_source_limiter_key("203.0.113.204")
    with db.db_connection(TEST_DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM admin_login_rate_limits WHERE limiter_key = %s", (key,))
            conn.commit()
        db.release_admin_login_admission(
            conn,
            limiter_key=key,
            now=now,
            rate_limit=5,
        )


@pytest.mark.unit
def test_session_from_row_normalizes_naive_expires_at() -> None:
    session = admin_auth.session_from_row(
        {
            "id": 1,
            "admin_username": "operator",
            "token_hash": "abc",
            "csrf_token_hash": None,
            "expires_at": datetime(2026, 1, 1, 12, 0, 0),
        }
    )
    assert session.expires_at.tzinfo is not None


@pytest.mark.unit
def test_resolve_login_limiter_keys_includes_account_for_configured_username(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_USERNAME", "operator")
    from app.config import get_settings

    settings = get_settings()
    keys = admin_auth.resolve_login_limiter_keys(
        submitted_username="Operator",
        client_source="203.0.113.1",
        settings=settings,
    )
    assert len(keys) == 2
    assert admin_auth.build_account_limiter_key(settings) in keys


@pytest.mark.unit
def test_admit_login_verification_reports_lockout_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    request = MagicMock()
    request.headers = {}
    request.client = MagicMock(host="203.0.113.5")
    from app.config import get_settings

    settings = get_settings()
    with (
        patch("app.admin_auth.db.db_connection") as db_conn,
        patch("app.admin_auth.db.admit_admin_login_attempt", return_value=(False, True)),
        patch("app.admin_auth.db.cleanup_expired_admin_login_rate_limits", return_value=0),
    ):
        db_conn.return_value.__enter__.return_value = MagicMock()
        db_conn.return_value.__exit__.return_value = None
        outcome = admin_auth.admit_login_verification(
            request,
            settings,
            username="operator",
        )
    assert not outcome.admitted
    assert outcome.lockout_transition


@pytest.mark.unit
def test_is_login_locked_uses_fallback_when_database_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    admin_auth.reset_login_rate_limiter()
    request = MagicMock()
    request.headers = {}
    request.client = MagicMock(host="203.0.113.5")
    from app.config import get_settings

    settings = get_settings()
    key = admin_auth.build_source_limiter_key("203.0.113.5")
    admin_auth._fallback_attempts[key] = (2, time.time())
    with patch("app.admin_auth.db.db_connection", side_effect=RuntimeError("db down")):
        assert admin_auth.is_login_locked(request, settings, username="ghost")


@pytest.mark.unit
def test_admit_login_verification_marks_already_locked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    request = MagicMock()
    request.headers = {}
    request.client = MagicMock(host="203.0.113.5")
    from app.config import get_settings

    settings = get_settings()
    with (
        patch("app.admin_auth.db.db_connection") as db_conn,
        patch("app.admin_auth.db.admit_admin_login_attempt", return_value=(False, False)),
        patch("app.admin_auth.db.cleanup_expired_admin_login_rate_limits", return_value=0),
    ):
        db_conn.return_value.__enter__.return_value = MagicMock()
        db_conn.return_value.__exit__.return_value = None
        outcome = admin_auth.admit_login_verification(
            request,
            settings,
            username="operator",
        )
    assert not outcome.admitted
    assert outcome.already_locked


@pytest.mark.unit
def test_read_session_token_rejects_blank_cookie() -> None:
    request = MagicMock()
    request.cookies = {admin_auth.SESSION_COOKIE_NAME: "   "}
    assert admin_auth.read_session_token(request) is None


@pytest.mark.unit
def test_read_login_flow_token_rejects_blank_cookie() -> None:
    request = MagicMock()
    request.cookies = {admin_auth.LOGIN_FLOW_COOKIE_NAME: "   "}
    assert admin_auth.read_login_flow_token(request) is None


@pytest.mark.unit
def test_admit_login_verification_granted_from_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    request = MagicMock()
    request.headers = {}
    request.client = MagicMock(host="203.0.113.5")
    from app.config import get_settings

    settings = get_settings()
    with (
        patch("app.admin_auth.db.db_connection") as db_conn,
        patch("app.admin_auth.db.admit_admin_login_attempt", return_value=(True, False)),
        patch("app.admin_auth.db.cleanup_expired_admin_login_rate_limits", return_value=0),
    ):
        db_conn.return_value.__enter__.return_value = MagicMock()
        db_conn.return_value.__exit__.return_value = None
        outcome = admin_auth.admit_login_verification(
            request,
            settings,
            username="operator",
        )
    assert outcome.admitted
    assert outcome.lockout_transition is False


@pytest.mark.unit
def test_is_login_locked_reads_database_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    request = MagicMock()
    request.headers = {}
    request.client = MagicMock(host="203.0.113.5")
    from app.config import get_settings

    settings = get_settings()
    with (
        patch("app.admin_auth.db.db_connection") as db_conn,
        patch("app.admin_auth.db.is_admin_login_locked", return_value=False) as locked,
    ):
        db_conn.return_value.__enter__.return_value = MagicMock()
        db_conn.return_value.__exit__.return_value = None
        assert not admin_auth.is_login_locked(request, settings, username="ghost")
    locked.assert_called_once()


@pytest.mark.unit
def test_fallback_admit_denies_when_limit_reached() -> None:
    admin_auth.reset_login_rate_limiter()
    key = "fallback-key"
    admin_auth._fallback_attempts[key] = (2, time.time())
    outcome = admin_auth._admit_fallback_failure([key])
    assert not outcome.admitted


@pytest.mark.unit
def test_is_fallback_throttled_expires_stale_window() -> None:
    admin_auth.reset_login_rate_limiter()
    key = "stale-key"
    admin_auth._fallback_attempts[key] = (5, time.time() - 120)
    assert not admin_auth._is_fallback_throttled([key])
    assert key not in admin_auth._fallback_attempts


@postgres_required
@pytest.mark.unit
def test_record_admin_login_failures_increments_shared_bucket() -> None:
    db.init_db(TEST_DATABASE_URL)
    now = datetime(2026, 3, 1, tzinfo=timezone.utc)
    key = admin_auth.build_source_limiter_key("203.0.113.201")
    with db.db_connection(TEST_DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM admin_login_rate_limits WHERE limiter_key = %s", (key,))
            conn.commit()
        db.record_admin_login_failures(
            conn,
            limiter_keys=[key],
            now=now,
            rate_limit=5,
            window_seconds=900,
            lockout_seconds=900,
        )
        assert db.is_admin_login_locked(conn, limiter_keys=[key], now=now) is False
        with conn.cursor() as cur:
            cur.execute(
                "SELECT failure_count FROM admin_login_rate_limits WHERE limiter_key = %s",
                (key,),
            )
            row = cur.fetchone()
    assert row is not None
    assert row["failure_count"] == 1


@postgres_required
@pytest.mark.unit
def test_admit_admin_login_attempt_denies_when_already_locked() -> None:
    db.init_db(TEST_DATABASE_URL)
    now = datetime(2026, 3, 1, tzinfo=timezone.utc)
    key = admin_auth.build_source_limiter_key("203.0.113.202")
    with db.db_connection(TEST_DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM admin_login_rate_limits WHERE limiter_key = %s", (key,))
            conn.commit()
        for _ in range(5):
            db.admit_admin_login_attempt(
                conn,
                limiter_keys=[key],
                now=now,
                rate_limit=5,
                window_seconds=900,
                lockout_seconds=900,
            )
        admitted, transition = db.admit_admin_login_attempt(
            conn,
            limiter_keys=[key],
            now=now,
            rate_limit=5,
            window_seconds=900,
            lockout_seconds=900,
        )
    assert not admitted
    assert not transition


@postgres_required
@pytest.mark.unit
def test_admit_admin_login_attempt_sets_lockout_transition() -> None:
    db.init_db(TEST_DATABASE_URL)
    now = datetime(2026, 3, 1, tzinfo=timezone.utc)
    key = admin_auth.build_source_limiter_key("203.0.113.203")
    with db.db_connection(TEST_DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM admin_login_rate_limits WHERE limiter_key = %s", (key,))
            conn.commit()
        for _ in range(4):
            db.admit_admin_login_attempt(
                conn,
                limiter_keys=[key],
                now=now,
                rate_limit=5,
                window_seconds=900,
                lockout_seconds=900,
            )
        admitted, transition = db.admit_admin_login_attempt(
            conn,
            limiter_keys=[key],
            now=now,
            rate_limit=5,
            window_seconds=900,
            lockout_seconds=900,
        )
    assert admitted
    assert transition


@pytest.mark.unit
def test_clear_login_rate_limit_releases_source_and_clears_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", "operator")
    request = MagicMock()
    request.headers = {}
    request.client = MagicMock(host="203.0.113.5")
    from app.config import get_settings

    settings = get_settings()
    with (
        patch("app.admin_auth.db.db_connection") as db_conn,
        patch("app.admin_auth.db.release_admin_login_admission") as release,
        patch("app.admin_auth.db.clear_admin_login_rate_limits") as clear_limits,
    ):
        db_conn.return_value.__enter__.return_value = MagicMock()
        db_conn.return_value.__exit__.return_value = None
        admin_auth.clear_login_rate_limit(request, settings)
    release.assert_called_once()
    clear_limits.assert_called_once()


@pytest.mark.unit
def test_is_login_throttled_delegates_to_lock_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    request = MagicMock()
    request.headers = {}
    request.client = MagicMock(host="203.0.113.5")
    from app.config import get_settings

    with patch("app.admin_auth.is_login_locked", return_value=True) as locked:
        assert admin_auth.is_login_throttled(request, get_settings(), username="operator")
    locked.assert_called_once()
