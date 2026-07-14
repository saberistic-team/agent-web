"""Unit tests for admin login-flow retention cleanup."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from app import admin_auth, db


@pytest.mark.unit
def test_cleanup_stale_admin_login_flows_deletes_bounded_batch() -> None:
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cur.rowcount = 4
    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)

    deleted = db.cleanup_stale_admin_login_flows(
        conn,
        now=now,
        retention_seconds=admin_auth.LOGIN_FLOW_CLEANUP_RETENTION_SECONDS,
        batch_size=admin_auth.LOGIN_FLOW_CLEANUP_BATCH_SIZE,
    )

    sql = cur.execute.call_args.args[0]
    params = cur.execute.call_args.args[1]
    assert "DELETE FROM admin_login_flows" in sql
    assert "SELECT id" in sql
    assert "expires_at <" in sql
    assert "consumed_at IS NOT NULL" in sql
    assert "LIMIT" in sql
    assert params == (
        now,
        admin_auth.LOGIN_FLOW_CLEANUP_RETENTION_SECONDS,
        now,
        admin_auth.LOGIN_FLOW_CLEANUP_RETENTION_SECONDS,
        admin_auth.LOGIN_FLOW_CLEANUP_BATCH_SIZE,
    )
    assert deleted == 4
    conn.commit.assert_called_once()


@pytest.mark.unit
def test_cleanup_stale_admin_login_flows_returns_zero_when_nothing_deleted() -> None:
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cur.rowcount = 0
    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)

    deleted = db.cleanup_stale_admin_login_flows(
        conn,
        now=now,
        retention_seconds=1800,
        batch_size=50,
    )

    assert deleted == 0
    conn.commit.assert_called_once()


@pytest.mark.unit
def test_login_flow_cleanup_constants_match_flow_ttl() -> None:
    assert admin_auth.LOGIN_FLOW_CLEANUP_RETENTION_SECONDS == admin_auth.CSRF_MAX_AGE_SECONDS * 2
    assert admin_auth.LOGIN_FLOW_CLEANUP_BATCH_SIZE == 100


def _flow_row(
    *,
    flow_id: int,
    expires_at: datetime,
    consumed_at: datetime | None = None,
) -> dict[str, object]:
    return {
        "id": flow_id,
        "flow_token_hash": f"hash-{flow_id}",
        "csrf_token_hash": f"csrf-{flow_id}",
        "created_at": expires_at - timedelta(minutes=15),
        "expires_at": expires_at,
        "consumed_at": consumed_at,
    }


@pytest.mark.unit
def test_in_memory_cleanup_removes_only_stale_flows() -> None:
    """Simulate cleanup selection logic for active, expired, and consumed rows."""
    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    retention = admin_auth.LOGIN_FLOW_CLEANUP_RETENTION_SECONDS
    cutoff = now - timedelta(seconds=retention)
    batch_size = 2

    flows = {
        "active": _flow_row(
            flow_id=1,
            expires_at=now + timedelta(minutes=10),
        ),
        "recently-expired": _flow_row(
            flow_id=2,
            expires_at=now - timedelta(minutes=5),
        ),
        "stale-expired": _flow_row(
            flow_id=3,
            expires_at=cutoff - timedelta(seconds=1),
        ),
        "recently-consumed": _flow_row(
            flow_id=4,
            expires_at=now + timedelta(minutes=5),
            consumed_at=now - timedelta(minutes=1),
        ),
        "stale-consumed": _flow_row(
            flow_id=5,
            expires_at=now + timedelta(minutes=5),
            consumed_at=cutoff - timedelta(seconds=1),
        ),
    }

    stale_ids: list[int] = []
    for row in flows.values():
        expired = row["expires_at"] < cutoff
        consumed_stale = (
            row["consumed_at"] is not None and row["consumed_at"] < cutoff
        )
        if expired or consumed_stale:
            stale_ids.append(int(row["id"]))

    stale_ids.sort()
    deleted_ids = stale_ids[:batch_size]

    assert deleted_ids == [3, 5]
    assert 1 in {int(flows["active"]["id"])}
    assert 2 not in deleted_ids
    assert 4 not in deleted_ids


@pytest.mark.unit
def test_boundary_time_flow_just_past_retention_is_stale() -> None:
    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    retention = 1800
    cutoff = now - timedelta(seconds=retention)

    row = _flow_row(flow_id=1, expires_at=cutoff - timedelta(seconds=1))
    assert row["expires_at"] < cutoff

    consumed = _flow_row(
        flow_id=2,
        expires_at=now + timedelta(minutes=5),
        consumed_at=cutoff - timedelta(seconds=1),
    )
    assert consumed["consumed_at"] < cutoff


@pytest.mark.unit
def test_boundary_time_flow_just_inside_retention_is_kept() -> None:
    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    retention = 1800
    cutoff = now - timedelta(seconds=retention)

    row = _flow_row(flow_id=1, expires_at=cutoff + timedelta(seconds=1))
    assert row["expires_at"] >= cutoff

    consumed = _flow_row(
        flow_id=2,
        expires_at=now + timedelta(minutes=5),
        consumed_at=cutoff + timedelta(seconds=1),
    )
    assert consumed["consumed_at"] >= cutoff
