"""Unit tests for shared admin login rate-limit database helpers."""

from __future__ import annotations

from datetime import datetime, timezone
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
def test_record_admin_login_failure_executes_upsert() -> None:
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    db.record_admin_login_failure(
        conn,
        limiter_key="key123",
        now=now,
        rate_limit=5,
        window_seconds=900,
        lockout_seconds=900,
    )

    sql = cur.execute.call_args.args[0]
    assert "INSERT INTO admin_login_rate_limits" in sql
    assert "ON CONFLICT (limiter_key) DO UPDATE" in sql
    conn.commit.assert_called_once()


@pytest.mark.unit
def test_clear_admin_login_rate_limit_deletes_row() -> None:
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur

    db.clear_admin_login_rate_limit(conn, limiter_key="key123")

    sql = cur.execute.call_args.args[0]
    assert "DELETE FROM admin_login_rate_limits" in sql
    assert cur.execute.call_args.args[1] == ("key123",)
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
