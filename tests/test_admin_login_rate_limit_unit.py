"""Unit tests for shared admin login rate-limit database helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from app import db


@pytest.mark.unit
def test_is_admin_login_throttled_false_when_no_row() -> None:
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cur.fetchone.return_value = None

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert not db.is_admin_login_throttled(conn, limiter_key="abc", now=now)


@pytest.mark.unit
def test_is_admin_login_throttled_true_when_locked() -> None:
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    cur.fetchone.return_value = {
        "locked_until": datetime(2026, 1, 1, 13, 0, tzinfo=timezone.utc),
    }

    assert db.is_admin_login_throttled(conn, limiter_key="abc", now=now)


@pytest.mark.unit
def test_is_admin_login_throttled_false_when_lockout_expired() -> None:
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    now = datetime(2026, 1, 1, 14, 0, tzinfo=timezone.utc)
    cur.fetchone.return_value = {
        "locked_until": datetime(2026, 1, 1, 13, 0, tzinfo=timezone.utc),
    }

    assert not db.is_admin_login_throttled(conn, limiter_key="abc", now=now)


@pytest.mark.unit
def test_try_admit_admin_login_executes_for_update_upsert() -> None:
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    cur.fetchall.return_value = [
        {
            "limiter_key": "key123",
            "failure_count": 0,
            "window_started_at": now,
            "locked_until": None,
        }
    ]

    admission = db.try_admit_admin_login(
        conn,
        limiter_keys=("key123",),
        now=now,
        rate_limit=5,
        window_seconds=900,
        lockout_seconds=900,
    )

    executed_sql = " ".join(call.args[0] for call in cur.execute.call_args_list)
    assert "INSERT INTO admin_login_rate_limits" in executed_sql
    assert "FOR UPDATE" in executed_sql
    assert "UPDATE admin_login_rate_limits" in executed_sql
    assert admission.admitted
    conn.commit.assert_called()


@pytest.mark.unit
def test_try_admit_admin_login_rejects_active_lockout_without_update() -> None:
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    cur.fetchall.return_value = [
        {
            "limiter_key": "key123",
            "failure_count": 5,
            "window_started_at": now - timedelta(minutes=1),
            "locked_until": datetime(2026, 1, 1, 13, 0, tzinfo=timezone.utc),
        }
    ]

    admission = db.try_admit_admin_login(
        conn,
        limiter_keys=("key123",),
        now=now,
        rate_limit=5,
        window_seconds=900,
        lockout_seconds=900,
    )

    executed_sql = " ".join(call.args[0] for call in cur.execute.call_args_list)
    assert "UPDATE admin_login_rate_limits" not in executed_sql
    assert not admission.admitted
    assert admission.already_locked


@pytest.mark.unit
def test_clear_admin_login_rate_limits_deletes_rows() -> None:
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur

    db.clear_admin_login_rate_limits(conn, limiter_keys=("key123", "key456"))

    sql = cur.execute.call_args.args[0]
    assert "DELETE FROM admin_login_rate_limits" in sql
    assert cur.execute.call_args.args[1] == (["key123", "key456"],)
    conn.commit.assert_called_once()


@pytest.mark.unit
def test_release_admin_login_admission_decrements_and_unlocks() -> None:
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    cur.fetchone.return_value = {
        "failure_count": 2,
        "locked_until": datetime(2026, 1, 1, 13, 0, tzinfo=timezone.utc),
    }

    db.release_admin_login_admission(
        conn,
        limiter_key="key123",
        now=now,
        rate_limit=5,
    )

    update_sql = cur.execute.call_args_list[-1].args[0]
    assert "UPDATE admin_login_rate_limits" in update_sql
    assert cur.execute.call_args_list[-1].args[1][0] == 1
    assert cur.execute.call_args_list[-1].args[1][1] is None
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
def test_try_admit_admin_login_empty_keys_admits() -> None:
    conn = MagicMock()
    admission = db.try_admit_admin_login(
        conn,
        limiter_keys=(),
        now=datetime(2026, 1, 1, tzinfo=timezone.utc),
        rate_limit=5,
        window_seconds=900,
        lockout_seconds=900,
    )
    assert admission.admitted
    assert not admission.throttled
    conn.cursor.assert_not_called()


@pytest.mark.unit
def test_try_admit_admin_login_resets_after_window_expiry() -> None:
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    cur.fetchall.return_value = [
        {
            "limiter_key": "key123",
            "failure_count": 4,
            "window_started_at": now - timedelta(seconds=901),
            "locked_until": None,
        }
    ]

    admission = db.try_admit_admin_login(
        conn,
        limiter_keys=("key123",),
        now=now,
        rate_limit=5,
        window_seconds=900,
        lockout_seconds=900,
    )

    assert admission.admitted
    assert not admission.lockout_transition
    update_args = cur.execute.call_args_list[-1].args[1]
    assert update_args[0] == 1
    assert update_args[1] == now


@pytest.mark.unit
def test_try_admit_admin_login_marks_lockout_transition_at_threshold() -> None:
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    cur.fetchall.return_value = [
        {
            "limiter_key": "key123",
            "failure_count": 4,
            "window_started_at": now - timedelta(minutes=1),
            "locked_until": None,
        }
    ]

    admission = db.try_admit_admin_login(
        conn,
        limiter_keys=("key123",),
        now=now,
        rate_limit=5,
        window_seconds=900,
        lockout_seconds=900,
    )

    assert admission.admitted
    assert admission.lockout_transition
    update_args = cur.execute.call_args_list[-1].args[1]
    assert update_args[0] == 5
    assert update_args[2] == now + timedelta(seconds=900)


@pytest.mark.unit
def test_try_admit_admin_login_allows_after_expired_lockout() -> None:
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    cur.fetchall.return_value = [
        {
            "limiter_key": "key123",
            "failure_count": 5,
            "window_started_at": now - timedelta(minutes=1),
            "locked_until": now - timedelta(seconds=1),
        }
    ]

    admission = db.try_admit_admin_login(
        conn,
        limiter_keys=("key123",),
        now=now,
        rate_limit=5,
        window_seconds=900,
        lockout_seconds=900,
    )

    assert admission.admitted
    assert admission.lockout_transition


@pytest.mark.unit
def test_release_admin_login_admission_noop_when_missing_row() -> None:
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cur.fetchone.return_value = None
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    db.release_admin_login_admission(
        conn,
        limiter_key="missing",
        now=now,
        rate_limit=5,
    )

    assert all("UPDATE admin_login_rate_limits" not in call.args[0] for call in cur.execute.call_args_list)
    conn.commit.assert_called_once()


@pytest.mark.unit
def test_is_login_throttled_falls_back_when_db_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import patch

    from starlette.requests import Request

    from app import admin_auth
    from app.config import get_settings

    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.setenv("ADMIN_USERNAME", "operator")
    monkeypatch.setenv(
        "ADMIN_PASSWORD_HASH",
        "$argon2id$v=19$m=65536,t=3,p=4$aaaaaaaaaaaaaaaaaaaaaa$bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    )
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "test-session-secret-32chars-minimum!!")
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", "test-limiter-secret-32chars-minimum!")
    monkeypatch.setenv("BASE_URL", "http://testserver")
    settings = get_settings()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/admin/login",
        "raw_path": b"/admin/login",
        "query_string": b"",
        "headers": [],
        "client": ("203.0.113.10", 12345),
        "server": ("testserver", 80),
    }
    request = Request(scope)

    with patch("app.admin_auth.db.db_connection", side_effect=RuntimeError("down")):
        with patch.object(admin_auth, "_is_fallback_throttled", return_value=True) as fallback:
            assert admin_auth.is_login_throttled(request, settings, username="operator")
            fallback.assert_called_once()


@pytest.mark.unit
def test_finalize_successful_login_clears_fallback_when_db_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import patch

    from starlette.requests import Request

    from app import admin_auth
    from app.config import get_settings

    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.setenv("ADMIN_USERNAME", "operator")
    monkeypatch.setenv(
        "ADMIN_PASSWORD_HASH",
        "$argon2id$v=19$m=65536,t=3,p=4$aaaaaaaaaaaaaaaaaaaaaa$bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    )
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "test-session-secret-32chars-minimum!!")
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", "test-limiter-secret-32chars-minimum!")
    monkeypatch.setenv("BASE_URL", "http://testserver")
    settings = get_settings()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/admin/login",
        "raw_path": b"/admin/login",
        "query_string": b"",
        "headers": [],
        "client": ("203.0.113.10", 12345),
        "server": ("testserver", 80),
    }
    request = Request(scope)

    with patch("app.admin_auth.db.db_connection", side_effect=RuntimeError("down")):
        with patch.object(admin_auth, "_clear_fallback_failures") as clear_fallback:
            with patch.object(admin_auth, "_release_fallback_admission") as release_fallback:
                admin_auth.finalize_successful_login(request, settings, username="operator")
                clear_fallback.assert_called_once()
                release_fallback.assert_called_once()
