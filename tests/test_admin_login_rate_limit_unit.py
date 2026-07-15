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
