"""Tests for keyed admin login limiter identifiers and anonymous failure actors."""

from __future__ import annotations

import hashlib
import logging
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Generator, Iterator
from unittest.mock import MagicMock, patch

import psycopg
import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from app import admin_auth, audit_service, db
from app.actor_context import ActorContext
from app.config import get_settings
from app.crm_uow import crm_transaction
from app.main import app
from app.migrations.runner import apply_migrations
from app.repositories.postgres import PostgresAuditEventRepository
from tests.conftest import TEST_LIMITER_SECRET

client = TestClient(app, follow_redirects=False)

_ORIGINAL_RECORD_LOGIN_FAILURE = audit_service.record_login_failure

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "test-session-secret-32chars-minimum"
ALT_LIMITER_SECRET = "alt-limiter-secret-32chars-minimum!!"
PREVIOUS_LIMITER_SECRET = "prev-limiter-secret-32chars-minimum"

_REQUIRED = (os.environ.get("REQUIRE_TEST_DATABASE") or "").strip() in {"1", "true", "yes"}
_DATABASE_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip()


@pytest.fixture(autouse=True)
def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.delenv("ADMIN_LOGIN_LIMITER_PREVIOUS_SECRET", raising=False)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    admin_auth.reset_login_rate_limiter()


@contextmanager
def mock_db_connection() -> Generator[MagicMock, None, None]:
    conn = MagicMock()
    with (
        patch("app.admin_routes.db.db_connection") as route_db,
        patch("app.admin_auth.db.db_connection") as auth_db,
    ):
        route_db.return_value.__enter__.return_value = conn
        route_db.return_value.__exit__.return_value = None
        auth_db.return_value.__enter__.return_value = conn
        auth_db.return_value.__exit__.return_value = None
        yield conn


class AuditSpy:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def append(self, _conn: Any, **kwargs: Any) -> dict[str, Any]:
        self.events.append(kwargs)
        return {"id": f"evt-{len(self.events)}"}


def _record_failure_with_spy(spy: AuditSpy, *args: Any, **kwargs: Any) -> Any:
    kwargs["repository"] = spy
    return _ORIGINAL_RECORD_LOGIN_FAILURE(*args, **kwargs)


def _require_database_url() -> str:
    if _DATABASE_URL:
        return _DATABASE_URL
    if _REQUIRED:
        pytest.fail("REQUIRE_TEST_DATABASE=1 but TEST_DATABASE_URL is unset")
    pytest.skip("TEST_DATABASE_URL not set; skipping live Postgres login limiter tests")


@pytest.fixture(scope="module")
def database_url() -> str:
    return _require_database_url()


@contextmanager
def _connect(database_url: str) -> Iterator[psycopg.Connection]:
    conn = psycopg.connect(database_url, row_factory=dict_row, autocommit=False)
    try:
        yield conn
    finally:
        conn.close()


def _reset_public_schema(conn: psycopg.Connection) -> None:
    conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
    conn.execute("CREATE SCHEMA public")
    conn.execute("GRANT ALL ON SCHEMA public TO CURRENT_USER")
    conn.execute("GRANT ALL ON SCHEMA public TO public")
    conn.commit()


@pytest.fixture
def pg_conn(database_url: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(database_url, autocommit=False) as bootstrap:
        _reset_public_schema(bootstrap)
        apply_migrations(bootstrap)
    with _connect(database_url) as conn:
        try:
            yield conn
        finally:
            conn.rollback()
            with psycopg.connect(database_url, autocommit=False) as cleanup:
                _reset_public_schema(cleanup)


@pytest.mark.unit
def test_limiter_identifier_is_not_plain_sha256() -> None:
    settings = get_settings()
    source = "203.0.113.50"
    keyed = admin_auth.build_source_rate_limit_key(source, settings=settings)
    plain = admin_auth._plain_sha256_limiter_identifier(
        admin_auth.LIMITER_DOMAIN_SOURCE,
        source.strip().lower(),
    )
    assert keyed != plain


@pytest.mark.unit
def test_limiter_identifier_depends_on_secret() -> None:
    settings = get_settings()
    source = "203.0.113.51"
    current = admin_auth.build_source_rate_limit_key(source, settings=settings)
    rotated = admin_auth.build_source_rate_limit_key(
        source,
        settings=settings,
        secret=ALT_LIMITER_SECRET,
    )
    assert current != rotated


@pytest.mark.unit
def test_limiter_identifier_is_stable_for_same_inputs() -> None:
    settings = get_settings()
    first = admin_auth.build_source_rate_limit_key("203.0.113.52", settings=settings)
    second = admin_auth.build_source_rate_limit_key("203.0.113.52", settings=settings)
    assert first == second
    assert len(first) == 64


@pytest.mark.unit
def test_limiter_identifier_separates_source_and_account_domains() -> None:
    settings = get_settings()
    material = "operator"
    source_key = admin_auth.build_source_rate_limit_key(material, settings=settings)
    account_key = admin_auth.build_account_rate_limit_key(material, settings=settings)
    assert source_key != account_key


@pytest.mark.unit
@pytest.mark.parametrize(
    ("secret", "field_name"),
    [
        ("", "ADMIN_LOGIN_LIMITER_SECRET"),
        ("short-secret", "ADMIN_LOGIN_LIMITER_SECRET"),
        ("changeme", "ADMIN_LOGIN_LIMITER_SECRET"),
        ("placeholder", "ADMIN_LOGIN_LIMITER_PREVIOUS_SECRET"),
    ],
)
def test_limiter_secret_validation_rejects_weak_material(
    secret: str,
    field_name: str,
) -> None:
    with pytest.raises(ValueError, match=field_name):
        admin_auth.validate_admin_login_limiter_secret(secret, field_name=field_name)


@pytest.mark.unit
def test_limiter_secret_validation_rejects_matching_previous_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_PREVIOUS_SECRET", TEST_LIMITER_SECRET)
    settings = get_settings()
    with pytest.raises(ValueError, match="must differ"):
        admin_auth.validate_admin_login_limiter_secrets(settings)


@pytest.mark.unit
def test_startup_validation_runs_when_admin_auth_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", "changeme")
    with pytest.raises(ValueError, match="ADMIN_LOGIN_LIMITER_SECRET"):
        with TestClient(app):
            pass


@pytest.mark.integration
def test_rotation_guard_key_blocks_without_incrementing_current(
    pg_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_PREVIOUS_SECRET", PREVIOUS_LIMITER_SECRET)
    settings = get_settings()
    now = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    previous_source = admin_auth.build_source_rate_limit_key(
        "203.0.113.60",
        settings=settings,
        secret=PREVIOUS_LIMITER_SECRET,
    )
    current_source = admin_auth.build_source_rate_limit_key(
        "203.0.113.60",
        settings=settings,
    )

    for index in range(5):
        admission = db.try_admit_admin_login(
            pg_conn,
            limiter_keys=(previous_source,),
            now=now + timedelta(seconds=index),
            rate_limit=5,
            window_seconds=900,
            lockout_seconds=900,
        )
        assert admission.admitted

    blocked = db.try_admit_admin_login(
        pg_conn,
        limiter_keys=(current_source,),
        guard_keys=(previous_source,),
        now=now + timedelta(seconds=10),
        rate_limit=5,
        window_seconds=900,
        lockout_seconds=900,
    )
    assert not blocked.admitted
    assert blocked.already_locked


@pytest.mark.integration
def test_rotation_cleanup_removes_expired_previous_key_rows(
    pg_conn: psycopg.Connection,
) -> None:
    settings = get_settings()
    now = datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc)
    previous_source = admin_auth.build_source_rate_limit_key(
        "203.0.113.61",
        settings=settings,
        secret=PREVIOUS_LIMITER_SECRET,
    )
    db.try_admit_admin_login(
        pg_conn,
        limiter_keys=(previous_source,),
        now=now - timedelta(hours=2),
        rate_limit=5,
        window_seconds=60,
        lockout_seconds=60,
    )
    deleted = db.cleanup_expired_admin_login_rate_limits(
        pg_conn,
        now=now,
        window_seconds=60,
        lockout_seconds=60,
    )
    assert deleted >= 1


@pytest.mark.integration
def test_atomic_admission_continues_with_hmac_keys(pg_conn: psycopg.Connection) -> None:
    settings = get_settings()
    now = datetime(2026, 5, 3, 9, 0, tzinfo=timezone.utc)
    source_key = admin_auth.build_source_rate_limit_key("198.51.100.88", settings=settings)
    rate_limit = 5
    barrier = threading.Barrier(8)
    admitted_count = {"value": 0}
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        with _connect(_DATABASE_URL) as conn:
            admission = db.try_admit_admin_login(
                conn,
                limiter_keys=(source_key,),
                now=now,
                rate_limit=rate_limit,
                window_seconds=900,
                lockout_seconds=900,
            )
            if admission.admitted:
                with lock:
                    admitted_count["value"] += 1

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert admitted_count["value"] == rate_limit


@pytest.mark.unit
@pytest.mark.integration
def test_unknown_username_failure_uses_anonymous_actor_only() -> None:
    spy = AuditSpy()
    admitted = admin_auth.LoginAdmissionResult(
        admitted=True,
        throttled=False,
        already_locked=False,
        lockout_transition=False,
    )
    with mock_db_connection():
        with patch("app.admin_routes._try_claim_login_flow", return_value=True):
            with patch(
                "app.admin_routes.admin_auth.try_admit_login_attempt",
                return_value=admitted,
            ):
                with patch(
                    "app.admin_routes.audit_service.record_login_failure",
                    side_effect=lambda *args, **kwargs: _record_failure_with_spy(
                        spy, *args, **kwargs
                    ),
                ):
                    candidate = "attacker-candidate@example.com"
                    response = client.post(
                        "/admin/login",
                        data={
                            "username": candidate,
                            "password": "wrong-password",
                            "csrf_token": "flow-csrf",
                        },
                    )
                    assert response.status_code == 401

    assert len(spy.events) == 1
    event = spy.events[0]
    assert event["actor"] == "anonymous"
    assert candidate not in str(event)


@pytest.mark.unit
@pytest.mark.integration
def test_configured_username_wrong_password_keeps_anonymous_actor() -> None:
    spy = AuditSpy()
    admitted = admin_auth.LoginAdmissionResult(
        admitted=True,
        throttled=False,
        already_locked=False,
        lockout_transition=False,
    )
    with mock_db_connection():
        with patch("app.admin_routes._try_claim_login_flow", return_value=True):
            with patch(
                "app.admin_routes.admin_auth.try_admit_login_attempt",
                return_value=admitted,
            ):
                with patch(
                    "app.admin_routes.audit_service.record_login_failure",
                    side_effect=lambda *args, **kwargs: _record_failure_with_spy(
                        spy, *args, **kwargs
                    ),
                ):
                    response = client.post(
                        "/admin/login",
                        data={
                            "username": TEST_USERNAME,
                            "password": "wrong-password",
                            "csrf_token": "flow-csrf",
                        },
                    )
                    assert response.status_code == 401

    event = spy.events[0]
    assert event["actor"] == "anonymous"
    assert TEST_USERNAME not in str(event["summary_after"])
    assert TEST_USERNAME not in str(event.get("metadata"))


@pytest.mark.unit
@pytest.mark.integration
def test_invalid_csrf_failure_uses_anonymous_actor() -> None:
    spy = AuditSpy()
    admitted = admin_auth.LoginAdmissionResult(
        admitted=True,
        throttled=False,
        already_locked=False,
        lockout_transition=False,
    )
    with mock_db_connection():
        with patch("app.admin_routes._try_claim_login_flow", return_value=False):
            with patch(
                "app.admin_routes.admin_auth.try_admit_login_attempt",
                return_value=admitted,
            ):
                with patch(
                    "app.admin_routes.audit_service.record_login_failure",
                    side_effect=lambda *args, **kwargs: _record_failure_with_spy(
                        spy, *args, **kwargs
                    ),
                ):
                    response = client.post(
                        "/admin/login",
                        data={
                            "username": "ghost-user",
                            "password": "wrong-password",
                            "csrf_token": "flow-csrf",
                        },
                    )
                    assert response.status_code == 400

    event = spy.events[0]
    assert event["actor"] == "anonymous"
    assert event["summary_after"]["reason"] == "invalid_csrf"


@pytest.mark.unit
@pytest.mark.integration
def test_lockout_transition_uses_anonymous_actor() -> None:
    spy = AuditSpy()
    with mock_db_connection():
        with patch("app.admin_routes._try_claim_login_flow", return_value=True):
            with patch(
                "app.admin_routes.admin_auth.try_admit_login_attempt",
                return_value=admin_auth.LoginAdmissionResult(
                    admitted=True,
                    throttled=False,
                    already_locked=False,
                    lockout_transition=True,
                ),
            ):
                with patch(
                    "app.admin_routes.audit_service.record_login_failure",
                    side_effect=lambda *args, **kwargs: _record_failure_with_spy(
                        spy, *args, **kwargs
                    ),
                ):
                    response = client.post(
                        "/admin/login",
                        data={
                            "username": TEST_USERNAME,
                            "password": "wrong-password",
                            "csrf_token": "flow-csrf",
                        },
                    )
                    assert response.status_code == 401

    event = spy.events[0]
    assert event["actor"] == "anonymous"
    assert event["summary_after"]["reason"] == "rate_limited"


@pytest.mark.unit
@pytest.mark.integration
def test_successful_login_retains_administrator_actor() -> None:
    admitted = admin_auth.LoginAdmissionResult(
        admitted=True,
        throttled=False,
        already_locked=False,
        lockout_transition=False,
    )
    with mock_db_connection():
        with patch("app.admin_routes._try_claim_login_flow", return_value=True):
            with patch(
                "app.admin_routes.admin_auth.try_admit_login_attempt",
                return_value=admitted,
            ):
                with patch("app.admin_routes.db.create_admin_session", return_value=42):
                    with patch(
                        "app.admin_routes.audit_service.record_login_success"
                    ) as success_audit:
                        response = client.post(
                            "/admin/login",
                            data={
                                "username": TEST_USERNAME,
                                "password": TEST_PASSWORD,
                                "csrf_token": "flow-csrf",
                            },
                        )
                        assert response.status_code == 303
                        success_audit.assert_called_once()
                        assert (
                            success_audit.call_args.kwargs["actor_context"].actor
                            == TEST_USERNAME
                        )
                        assert success_audit.call_args.kwargs["session_id"] == 42


@pytest.mark.unit
def test_login_failure_logs_do_not_contain_candidate_or_secret(
    caplog: pytest.LogCaptureFixture,
) -> None:
    candidate = "candidate-user@evil.example"
    secret = TEST_LIMITER_SECRET
    source = "203.0.113.99"
    with caplog.at_level(logging.WARNING):
        try:
            admin_auth.validate_admin_login_limiter_secret("changeme")
        except ValueError:
            pass
        admin_auth.build_source_rate_limit_key(source, settings=get_settings())
        admin_auth._plain_sha256_limiter_identifier(
            admin_auth.LIMITER_DOMAIN_SOURCE,
            source,
        )

    joined = " ".join(record.getMessage() for record in caplog.records)
    assert candidate not in joined
    assert secret not in joined
    assert source not in joined
    assert "src:" not in joined
    assert "acct:" not in joined


@pytest.mark.integration
def test_postgres_rows_store_keyed_identifiers_and_anonymous_actors(
    pg_conn: psycopg.Connection,
) -> None:
    settings = get_settings()
    source = "203.0.113.70"
    keyed = admin_auth.build_source_rate_limit_key(source, settings=settings)
    plain = hashlib.sha256(f"src:{source}".encode("utf-8")).hexdigest()
    now = datetime(2026, 5, 4, 10, 0, tzinfo=timezone.utc)

    db.try_admit_admin_login(
        pg_conn,
        limiter_keys=(keyed,),
        now=now,
        rate_limit=5,
        window_seconds=900,
        lockout_seconds=900,
    )

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT limiter_key FROM admin_login_rate_limits WHERE limiter_key = %s",
            (keyed,),
        )
        row = cur.fetchone()
    assert row is not None
    assert row["limiter_key"] == keyed
    assert row["limiter_key"] != plain

    spy = PostgresAuditEventRepository()
    actor = ActorContext(actor="anonymous", correlation_id="corr-pg")
    with crm_transaction(pg_conn):
        audit_service.record_login_failure(
            pg_conn,
            actor_context=actor,
            reason="invalid_credentials",
            repository=spy,
        )

    with pg_conn.cursor() as cur:
        cur.execute(
            """
            SELECT actor, summary_after, metadata
            FROM audit_events
            WHERE action = %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (audit_service.ACTION_AUTH_LOGIN_FAILURE,),
        )
        audit_row = cur.fetchone()
    assert audit_row is not None
    assert audit_row["actor"] == "anonymous"
    assert "operator" not in str(audit_row["summary_after"])
    assert "operator" not in str(audit_row["metadata"])
