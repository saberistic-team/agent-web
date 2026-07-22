"""Tests for discovery run advisory locking."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.discovery.run_lock import (
    DISCOVERY_RUN_ADVISORY_LOCK_KEY1,
    DISCOVERY_RUN_ADVISORY_LOCK_KEY2,
    DiscoveryRunLock,
)


@pytest.mark.unit
@pytest.mark.integration
def test_discovery_run_lock_acquire_and_release() -> None:
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.side_effect = [(True,), (True,)]

    lock = DiscoveryRunLock(conn)
    assert lock.try_acquire() is True
    assert lock.held is True
    lock.release()
    assert lock.held is False

    acquire_sql = cur.execute.call_args_list[0].args[0]
    assert "pg_try_advisory_lock" in acquire_sql
    assert cur.execute.call_args_list[0].args[1] == (
        DISCOVERY_RUN_ADVISORY_LOCK_KEY1,
        DISCOVERY_RUN_ADVISORY_LOCK_KEY2,
    )


@pytest.mark.unit
@pytest.mark.integration
def test_discovery_run_lock_not_acquired() -> None:
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value = (False,)

    lock = DiscoveryRunLock(conn)
    assert lock.try_acquire() is False
    assert lock.held is False
    lock.release()
    cur.execute.assert_called_once()
