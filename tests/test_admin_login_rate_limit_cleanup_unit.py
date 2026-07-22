"""Unit tests for bounded admin login limiter cleanup (#332)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from app import admin_auth, db


def _now() -> datetime:
    return datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)


@pytest.mark.unit
def test_cleanup_expired_admin_login_rate_limits_rejects_non_positive_batch() -> None:
    conn = MagicMock()
    with pytest.raises(ValueError, match="batch_size must be positive"):
        db.cleanup_expired_admin_login_rate_limits(
            conn,
            now=_now(),
            window_seconds=900,
            lockout_seconds=900,
            batch_size=0,
        )


@pytest.mark.unit
def test_has_expired_admin_login_rate_limits_returns_exists_result() -> None:
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cur.fetchone.return_value = {"has_backlog": True}

    assert db.has_expired_admin_login_rate_limits(
        conn,
        now=_now(),
        window_seconds=900,
        lockout_seconds=900,
    )
    sql = cur.execute.call_args.args[0]
    assert "SELECT EXISTS" in sql


class FakeLimiterCleanupStore:
    """In-memory stand-in for bounded limiter cleanup behavior."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, object]] = {}

    def insert(
        self,
        *,
        limiter_key: str,
        updated_at: datetime,
        locked_until: datetime | None = None,
    ) -> None:
        self.rows[limiter_key] = {
            "updated_at": updated_at,
            "locked_until": locked_until,
        }

    def cleanup(
        self,
        now: datetime,
        *,
        window_seconds: int,
        lockout_seconds: int,
        batch_size: int,
    ) -> int:
        retention = max(window_seconds, lockout_seconds) * 2
        cutoff = now - timedelta(seconds=retention)
        eligible = sorted(
            (
                key
                for key, row in self.rows.items()
                if row["updated_at"] < cutoff  # type: ignore[operator]
                and (
                    row["locked_until"] is None
                    or row["locked_until"] < now  # type: ignore[operator]
                )
            ),
            key=lambda key: (self.rows[key]["updated_at"], key),  # type: ignore[index]
        )[:batch_size]
        for key in eligible:
            del self.rows[key]
        return len(eligible)


@pytest.mark.unit
def test_fake_store_bounded_batch_deletes_oldest_first() -> None:
    store = FakeLimiterCleanupStore()
    now = _now()
    retention = 120
    for index in range(3):
        store.insert(
            limiter_key=f"stale-{index}",
            updated_at=now - timedelta(seconds=retention + index + 1),
        )
    deleted = store.cleanup(
        now,
        window_seconds=60,
        lockout_seconds=60,
        batch_size=2,
    )
    assert deleted == 2
    assert len(store.rows) == 1
    assert "stale-0" in store.rows


@pytest.mark.unit
def test_fake_store_preserves_active_recent_and_locked_rows() -> None:
    store = FakeLimiterCleanupStore()
    now = _now()
    retention = 120
    store.insert(
        limiter_key="stale",
        updated_at=now - timedelta(seconds=retention + 5),
    )
    store.insert(
        limiter_key="recent",
        updated_at=now - timedelta(seconds=retention - 1),
    )
    store.insert(
        limiter_key="locked",
        updated_at=now - timedelta(seconds=retention + 5),
        locked_until=now + timedelta(minutes=1),
    )
    deleted = store.cleanup(
        now,
        window_seconds=60,
        lockout_seconds=60,
        batch_size=10,
    )
    assert deleted == 1
    assert set(store.rows) == {"recent", "locked"}


@pytest.mark.unit
def test_try_admit_login_cleanup_failure_does_not_block_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from starlette.requests import Request

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
    admission = db.AdminLoginAdmission(
        admitted=True,
        throttled=False,
        already_locked=False,
        lockout_transition=False,
    )

    with (
        patch("app.admin_auth.db.db_connection") as db_conn,
        patch("app.admin_auth.db.try_admit_admin_login", return_value=admission),
        patch(
            "app.admin_auth.db.cleanup_expired_admin_login_rate_limits",
            side_effect=RuntimeError("cleanup failed"),
        ),
    ):
        db_conn.return_value.__enter__.return_value = MagicMock()
        db_conn.return_value.__exit__.return_value = None
        result = admin_auth.try_admit_login_attempt(request, settings, username="operator")

    assert result.admitted
    assert not result.store_unavailable
