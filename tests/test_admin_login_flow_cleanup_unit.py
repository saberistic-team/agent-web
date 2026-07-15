"""Unit tests for admin login-flow retention cleanup."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from app import admin_auth, db


def _now() -> datetime:
    return datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)


@pytest.mark.unit
def test_cleanup_stale_admin_login_flows_deletes_expired_and_consumed() -> None:
    import importlib

    import app.db as db_module

    importlib.reload(db_module)
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cur.rowcount = 4
    now = _now()

    deleted = db_module.cleanup_stale_admin_login_flows(
        conn,
        now=now,
        expired_retention_seconds=admin_auth.LOGIN_FLOW_EXPIRED_RETENTION_SECONDS,
        consumed_retention_seconds=admin_auth.LOGIN_FLOW_CONSUMED_RETENTION_SECONDS,
        batch_size=admin_auth.LOGIN_FLOW_CLEANUP_BATCH_SIZE,
    )

    sql = cur.execute.call_args.args[0]
    assert "DELETE FROM admin_login_flows" in sql
    assert "consumed_at IS NULL" in sql
    assert "consumed_at IS NOT NULL" in sql
    assert "LIMIT %s" in sql
    assert cur.execute.call_args.args[1] == (
        now,
        admin_auth.LOGIN_FLOW_EXPIRED_RETENTION_SECONDS,
        now,
        admin_auth.LOGIN_FLOW_CONSUMED_RETENTION_SECONDS,
        admin_auth.LOGIN_FLOW_CLEANUP_BATCH_SIZE,
    )
    assert deleted == 4
    conn.commit.assert_called_once()


@pytest.mark.unit
def test_cleanup_stale_admin_login_flows_respects_batch_size() -> None:
    import importlib

    import app.db as db_module

    importlib.reload(db_module)
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cur.rowcount = 50
    now = _now()

    deleted = db_module.cleanup_stale_admin_login_flows(
        conn,
        now=now,
        expired_retention_seconds=admin_auth.LOGIN_FLOW_EXPIRED_RETENTION_SECONDS,
        consumed_retention_seconds=admin_auth.LOGIN_FLOW_CONSUMED_RETENTION_SECONDS,
        batch_size=50,
    )

    assert cur.execute.call_args.args[1][-1] == 50
    assert deleted == 50


class FakeLoginFlowStore:
    """In-memory stand-in for admin_login_flows cleanup behavior."""

    def __init__(self) -> None:
        self.rows: dict[int, dict[str, object]] = {}
        self._next_id = 0

    def insert(
        self,
        *,
        flow_token_hash: str,
        csrf_token_hash: str,
        expires_at: datetime,
        consumed_at: datetime | None = None,
    ) -> int:
        self._next_id += 1
        row_id = self._next_id
        self.rows[row_id] = {
            "id": row_id,
            "flow_token_hash": flow_token_hash,
            "csrf_token_hash": csrf_token_hash,
            "expires_at": expires_at,
            "consumed_at": consumed_at,
        }
        return row_id

    def cleanup(
        self,
        now: datetime,
        *,
        expired_retention_seconds: int,
        consumed_retention_seconds: int,
        batch_size: int,
    ) -> int:
        expired_cutoff = now - timedelta(seconds=expired_retention_seconds)
        consumed_cutoff = now - timedelta(seconds=consumed_retention_seconds)
        stale_ids = sorted(
            row_id
            for row_id, row in self.rows.items()
            if (
                row["consumed_at"] is None
                and row["expires_at"] < expired_cutoff
            )
            or (
                row["consumed_at"] is not None
                and row["consumed_at"] < consumed_cutoff  # type: ignore[operator]
            )
        )[:batch_size]
        for row_id in stale_ids:
            del self.rows[row_id]
        return len(stale_ids)


@pytest.mark.unit
def test_fake_store_preserves_active_flows() -> None:
    store = FakeLoginFlowStore()
    now = _now()
    store.insert(
        flow_token_hash="active",
        csrf_token_hash="csrf-active",
        expires_at=now + timedelta(minutes=10),
    )
    store.cleanup(
        now,
        expired_retention_seconds=admin_auth.LOGIN_FLOW_EXPIRED_RETENTION_SECONDS,
        consumed_retention_seconds=admin_auth.LOGIN_FLOW_CONSUMED_RETENTION_SECONDS,
        batch_size=admin_auth.LOGIN_FLOW_CLEANUP_BATCH_SIZE,
    )
    assert len(store.rows) == 1


@pytest.mark.unit
def test_fake_store_deletes_expired_after_retention() -> None:
    store = FakeLoginFlowStore()
    now = _now()
    retention = admin_auth.LOGIN_FLOW_EXPIRED_RETENTION_SECONDS
    store.insert(
        flow_token_hash="expired",
        csrf_token_hash="csrf-expired",
        expires_at=now - timedelta(seconds=retention + 1),
    )
    deleted = store.cleanup(
        now,
        expired_retention_seconds=retention,
        consumed_retention_seconds=admin_auth.LOGIN_FLOW_CONSUMED_RETENTION_SECONDS,
        batch_size=admin_auth.LOGIN_FLOW_CLEANUP_BATCH_SIZE,
    )
    assert deleted == 1
    assert not store.rows


@pytest.mark.unit
def test_fake_store_keeps_expired_inside_retention_window() -> None:
    store = FakeLoginFlowStore()
    now = _now()
    retention = admin_auth.LOGIN_FLOW_EXPIRED_RETENTION_SECONDS
    store.insert(
        flow_token_hash="boundary",
        csrf_token_hash="csrf-boundary",
        expires_at=now - timedelta(seconds=retention - 1),
    )
    deleted = store.cleanup(
        now,
        expired_retention_seconds=retention,
        consumed_retention_seconds=admin_auth.LOGIN_FLOW_CONSUMED_RETENTION_SECONDS,
        batch_size=admin_auth.LOGIN_FLOW_CLEANUP_BATCH_SIZE,
    )
    assert deleted == 0
    assert len(store.rows) == 1


@pytest.mark.unit
def test_fake_store_deletes_consumed_after_retention() -> None:
    store = FakeLoginFlowStore()
    now = _now()
    retention = admin_auth.LOGIN_FLOW_CONSUMED_RETENTION_SECONDS
    store.insert(
        flow_token_hash="consumed",
        csrf_token_hash="csrf-consumed",
        expires_at=now + timedelta(minutes=5),
        consumed_at=now - timedelta(seconds=retention + 1),
    )
    deleted = store.cleanup(
        now,
        expired_retention_seconds=admin_auth.LOGIN_FLOW_EXPIRED_RETENTION_SECONDS,
        consumed_retention_seconds=retention,
        batch_size=admin_auth.LOGIN_FLOW_CLEANUP_BATCH_SIZE,
    )
    assert deleted == 1
    assert not store.rows


@pytest.mark.unit
def test_fake_store_keeps_consumed_inside_retention_window() -> None:
    store = FakeLoginFlowStore()
    now = _now()
    retention = admin_auth.LOGIN_FLOW_CONSUMED_RETENTION_SECONDS
    store.insert(
        flow_token_hash="recent-consumed",
        csrf_token_hash="csrf-recent",
        expires_at=now + timedelta(minutes=5),
        consumed_at=now - timedelta(seconds=retention - 1),
    )
    deleted = store.cleanup(
        now,
        expired_retention_seconds=admin_auth.LOGIN_FLOW_EXPIRED_RETENTION_SECONDS,
        consumed_retention_seconds=retention,
        batch_size=admin_auth.LOGIN_FLOW_CLEANUP_BATCH_SIZE,
    )
    assert deleted == 0
    assert len(store.rows) == 1


@pytest.mark.unit
def test_fake_store_bounded_batch_deletes_oldest_first() -> None:
    store = FakeLoginFlowStore()
    now = _now()
    retention = admin_auth.LOGIN_FLOW_EXPIRED_RETENTION_SECONDS
    for index in range(3):
        store.insert(
            flow_token_hash=f"stale-{index}",
            csrf_token_hash=f"csrf-{index}",
            expires_at=now - timedelta(seconds=retention + index + 1),
        )
    deleted = store.cleanup(
        now,
        expired_retention_seconds=retention,
        consumed_retention_seconds=admin_auth.LOGIN_FLOW_CONSUMED_RETENTION_SECONDS,
        batch_size=2,
    )
    assert deleted == 2
    assert len(store.rows) == 1
    remaining = next(iter(store.rows.values()))
    assert remaining["flow_token_hash"] == "stale-2"
