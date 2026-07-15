"""Tests for keyed admin login limiter identifiers and anonymous failure actors."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Generator
from unittest.mock import MagicMock, patch

import psycopg
import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from app import admin_auth, audit_service, db
from app.admin_auth import (
    LIMITER_DOMAIN_ACCOUNT,
    LIMITER_DOMAIN_SOURCE,
    LIMITER_KEY_HEX_LENGTH,
)
from app.config import Settings, get_settings
from app.crm_uow import crm_transaction
from app.main import app
from app.migrations.runner import apply_migrations
from app.repositories.postgres import PostgresAuditEventRepository
from tests.test_admin_auth import FakeRateLimitStore, mock_db_connection, shared_rate_limiter

client = TestClient(app, follow_redirects=False)

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SESSION_SECRET = "test-session-secret-32chars-minimum"
TEST_LIMITER_SECRET = "test-limiter-secret-32chars-minimum!!"
TEST_LIMITER_SECRET_ALT = "alt-limiter-secret-32chars-minimum!!"
TEST_LIMITER_SECRET_PREVIOUS = "prev-limiter-secret-32chars-minimum!"
ATTACKER_USERNAME = "attacker-candidate@evil.example"

_REQUIRED = (os.environ.get("REQUIRE_TEST_DATABASE") or "").strip() in {"1", "true", "yes"}
_DATABASE_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip()


def _settings(**overrides: str) -> Settings:
    env = {
        "database_url": "postgresql://test:test@localhost:5432/test",
        "stripe_secret_key": "",
        "stripe_webhook_secret": "",
        "stripe_publishable_key": "",
        "resend_api_key": "",
        "from_email": "noreply@example.com",
        "notify_email": "inbox@example.com",
        "base_url": "http://testserver",
        "plausible_domain": "",
        "plausible_api_key": "",
        "analytics_environment": "development",
        "admin_username": TEST_USERNAME,
        "admin_password_hash": TEST_HASH,
        "admin_session_secret": TEST_SESSION_SECRET,
        "admin_login_limiter_secret": TEST_LIMITER_SECRET,
        "admin_login_limiter_secret_previous": "",
    }
    env.update(overrides)
    return Settings(**env)


@pytest.fixture(autouse=True)
def limiter_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SESSION_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.delenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", raising=False)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    admin_auth.reset_login_rate_limiter()


def _plain_sha256_limiter_key(domain: str, material: str) -> str:
    payload = f"{domain}:{material}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@pytest.mark.unit
def test_limiter_identifier_is_not_plain_sha256() -> None:
    settings = _settings()
    source_key = admin_auth.build_source_rate_limit_key("203.0.113.1", settings)
    account_key = admin_auth.build_account_rate_limit_key("operator", settings)
    assert source_key != _plain_sha256_limiter_key("src", "203.0.113.1")
    assert account_key != _plain_sha256_limiter_key("acct", "operator")
    assert len(source_key) == LIMITER_KEY_HEX_LENGTH
    assert len(account_key) == LIMITER_KEY_HEX_LENGTH


@pytest.mark.unit
def test_limiter_identifier_depends_on_secret() -> None:
    settings_a = _settings(admin_login_limiter_secret=TEST_LIMITER_SECRET)
    settings_b = _settings(admin_login_limiter_secret=TEST_LIMITER_SECRET_ALT)
    key_a = admin_auth.build_source_rate_limit_key("203.0.113.1", settings_a)
    key_b = admin_auth.build_source_rate_limit_key("203.0.113.1", settings_b)
    assert key_a != key_b


@pytest.mark.unit
def test_limiter_identifier_is_stable_for_same_secret_and_input() -> None:
    settings = _settings()
    first = admin_auth.build_source_rate_limit_key("203.0.113.1", settings)
    second = admin_auth.build_source_rate_limit_key("203.0.113.1", settings)
    assert first == second


@pytest.mark.unit
def test_limiter_domain_separation() -> None:
    settings = _settings()
    shared_material = "203.0.113.1"
    source_key = admin_auth._digest_limiter_key(
        TEST_LIMITER_SECRET,
        LIMITER_DOMAIN_SOURCE,
        shared_material,
    )
    account_key = admin_auth._digest_limiter_key(
        TEST_LIMITER_SECRET,
        LIMITER_DOMAIN_ACCOUNT,
        shared_material,
    )
    assert source_key != account_key
    assert (
        admin_auth.build_source_rate_limit_key(shared_material, settings)
        != admin_auth.build_account_rate_limit_key(shared_material, settings)
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("secret", "env_name", "message"),
    [
        ("", "ADMIN_LOGIN_LIMITER_SECRET", "required"),
        ("short-secret", "ADMIN_LOGIN_LIMITER_SECRET", "at least 32"),
        (" changeme-is-long-enough-for-length-check!!", "ADMIN_LOGIN_LIMITER_SECRET", "leading or trailing whitespace"),
        ("placeholder-placeholder-placeholder!", "ADMIN_LOGIN_LIMITER_SECRET", "placeholder"),
        ("test-limiter-secret-32chars-minimum!!\x00", "ADMIN_LOGIN_LIMITER_SECRET", "control characters"),
    ],
)
def test_limiter_secret_validation_rejects_weak_material(
    secret: str,
    env_name: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        admin_auth.validate_admin_login_limiter_secret(secret, env_name=env_name)


@pytest.mark.unit
def test_validate_admin_security_config_rejects_matching_previous_secret() -> None:
    settings = _settings(
        admin_login_limiter_secret=TEST_LIMITER_SECRET,
        admin_login_limiter_secret_previous=TEST_LIMITER_SECRET,
    )
    with pytest.raises(ValueError, match="must differ"):
        admin_auth.validate_admin_security_config(settings)


@pytest.mark.unit
def test_startup_validation_requires_limiter_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ADMIN_LOGIN_LIMITER_SECRET", raising=False)
    settings = get_settings()
    with pytest.raises(ValueError, match="ADMIN_LOGIN_LIMITER_SECRET is required"):
        admin_auth.validate_admin_security_config(settings)


@pytest.mark.unit
def test_rotation_previous_key_blocks_until_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", TEST_LIMITER_SECRET_PREVIOUS)
    current_settings = get_settings()
    previous_settings = _settings(
        admin_login_limiter_secret=TEST_LIMITER_SECRET_PREVIOUS,
        admin_login_limiter_secret_previous="",
    )

    source = "testclient"
    previous_key = admin_auth.build_source_rate_limit_key(source, previous_settings)
    current_key = admin_auth.build_source_rate_limit_key(source, current_settings)
    assert previous_key != current_key

    store = _FakeRateLimitStore()
    now = datetime.now(timezone.utc)
    store.rows[previous_key] = {
        "failure_count": 5,
        "window_started_at": now,
        "locked_until": now + timedelta(minutes=15),
        "updated_at": now,
    }

    with _shared_rate_limiter(store):
        request = _login_request()
        admission = admin_auth.try_admit_login_attempt(
            request,
            current_settings,
            username=TEST_USERNAME,
        )
    assert not admission.admitted
    assert admission.already_locked
    assert current_key not in store.rows


@pytest.mark.unit
def test_rotation_cleanup_removes_stale_previous_key_rows() -> None:
    settings = _settings(
        admin_login_limiter_secret=TEST_LIMITER_SECRET,
        admin_login_limiter_secret_previous=TEST_LIMITER_SECRET_PREVIOUS,
    )
    previous_settings = _settings(
        admin_login_limiter_secret=TEST_LIMITER_SECRET_PREVIOUS,
        admin_login_limiter_secret_previous="",
    )
    source = "testclient"
    previous_key = admin_auth.build_source_rate_limit_key(source, previous_settings)
    store = _FakeRateLimitStore()
    stale = datetime.now(timezone.utc) - timedelta(hours=2)
    store.rows[previous_key] = {
        "failure_count": 1,
        "window_started_at": stale,
        "locked_until": None,
        "updated_at": stale,
    }

    with _shared_rate_limiter(store):
        request = _login_request()
        admin_auth.try_admit_login_attempt(request, settings, username=TEST_USERNAME)

    assert previous_key not in store.rows


class _FakeRateLimitStore:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    def is_throttled(self, limiter_key: str, now: datetime) -> bool:
        row = self.rows.get(limiter_key)
        if row is None:
            return False
        locked_until = row.get("locked_until")
        return locked_until is not None and locked_until > now

    def try_admit(
        self,
        limiter_keys: tuple[str, ...],
        now: datetime,
        *,
        rate_limit: int,
        window_seconds: int,
        lockout_seconds: int,
    ) -> db.AdminLoginAdmission:
        ordered_keys = tuple(sorted(limiter_keys))
        for limiter_key in ordered_keys:
            if self.is_throttled(limiter_key, now):
                return db.AdminLoginAdmission(
                    admitted=False,
                    throttled=True,
                    already_locked=True,
                    lockout_transition=False,
                )
        for limiter_key in ordered_keys:
            row = self.rows.get(limiter_key)
            if row is None:
                self.rows[limiter_key] = {
                    "failure_count": 1,
                    "window_started_at": now,
                    "locked_until": None,
                    "updated_at": now,
                }
            else:
                row["failure_count"] = int(row["failure_count"]) + 1
                row["updated_at"] = now
        return db.AdminLoginAdmission(
            admitted=True,
            throttled=False,
            already_locked=False,
            lockout_transition=False,
        )

    def cleanup(
        self,
        now: datetime,
        *,
        window_seconds: int,
        lockout_seconds: int,
    ) -> int:
        retention = max(window_seconds, lockout_seconds) * 2
        cutoff = now - timedelta(seconds=retention)
        expired = [
            key
            for key, row in self.rows.items()
            if row["updated_at"] < cutoff
            and (row["locked_until"] is None or row["locked_until"] < now)
        ]
        for key in expired:
            del self.rows[key]
        return len(expired)


@contextmanager
def _shared_rate_limiter(store: _FakeRateLimitStore) -> Generator[None, None, None]:
    def is_throttled(conn: Any, *, limiter_key: str, now: datetime) -> bool:
        return store.is_throttled(limiter_key, now)

    def try_admit(
        conn: Any,
        *,
        limiter_keys: tuple[str, ...],
        now: datetime,
        rate_limit: int,
        window_seconds: int,
        lockout_seconds: int,
    ) -> db.AdminLoginAdmission:
        return store.try_admit(
            limiter_keys,
            now,
            rate_limit=rate_limit,
            window_seconds=window_seconds,
            lockout_seconds=lockout_seconds,
        )

    def cleanup(
        conn: Any,
        *,
        now: datetime,
        window_seconds: int,
        lockout_seconds: int,
    ) -> int:
        return store.cleanup(
            now,
            window_seconds=window_seconds,
            lockout_seconds=lockout_seconds,
        )

    with (
        patch("app.admin_auth.db.is_admin_login_throttled", side_effect=is_throttled),
        patch("app.admin_auth.db.try_admit_admin_login", side_effect=try_admit),
        patch(
            "app.admin_auth.db.cleanup_expired_admin_login_rate_limits",
            side_effect=cleanup,
        ),
        patch("app.admin_auth.db.db_connection") as db_conn,
    ):
        db_conn.return_value.__enter__.return_value = MagicMock()
        db_conn.return_value.__exit__.return_value = None
        yield


def _login_request() -> Any:
    from starlette.requests import Request

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
        "client": ("testclient", 12345),
        "server": ("testserver", 80),
    }
    return Request(scope)


@pytest.mark.unit
@pytest.mark.integration
def test_unknown_username_failure_audit_uses_anonymous_actor() -> None:
    repo = MagicMock()
    repo.append.return_value = {"id": "evt-failure"}
    store = FakeRateLimitStore()
    with shared_rate_limiter(store):
        with mock_db_connection():
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                with patch("app.audit_service.get_repositories") as get_repos:
                    get_repos.return_value.audit_events = repo
                    response = client.post(
                        "/admin/login",
                        data={
                            "username": ATTACKER_USERNAME,
                            "password": "wrong-password",
                            "csrf_token": "flow-csrf",
                        },
                    )
                    assert response.status_code == 401
                    repo.append.assert_called_once()
                    assert repo.append.call_args.kwargs["actor"] == "anonymous"
                    metadata = repo.append.call_args.kwargs["metadata"]
                    assert metadata == {"reason": "invalid_credentials"}
                    serialized = json.dumps(repo.append.call_args.kwargs)
                    assert ATTACKER_USERNAME not in serialized


@pytest.mark.unit
@pytest.mark.integration
def test_configured_username_wrong_password_keeps_anonymous_actor() -> None:
    repo = MagicMock()
    repo.append.return_value = {"id": "evt-failure"}
    store = FakeRateLimitStore()
    with shared_rate_limiter(store):
        with mock_db_connection():
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                with patch("app.audit_service.get_repositories") as get_repos:
                    get_repos.return_value.audit_events = repo
                    response = client.post(
                        "/admin/login",
                        data={
                            "username": TEST_USERNAME,
                            "password": "wrong-password",
                            "csrf_token": "flow-csrf",
                        },
                    )
                    assert response.status_code == 401
                    assert repo.append.call_args.kwargs["actor"] == "anonymous"


@pytest.mark.unit
@pytest.mark.integration
def test_invalid_flow_and_lockout_audit_events_use_anonymous_actor() -> None:
    repo = MagicMock()
    repo.append.return_value = {"id": "evt-failure"}
    store = FakeRateLimitStore()
    with shared_rate_limiter(store):
        with mock_db_connection():
            with patch("app.admin_routes._try_claim_login_flow", return_value=False):
                with patch("app.admin_routes._try_burn_login_flow_cookie", return_value=True):
                    with patch("app.audit_service.get_repositories") as get_repos:
                        get_repos.return_value.audit_events = repo
                        invalid_flow = client.post(
                            "/admin/login",
                            data={
                                "username": ATTACKER_USERNAME,
                                "password": "wrong-password",
                                "csrf_token": "flow-csrf",
                            },
                        )
                        assert invalid_flow.status_code == 400
                        assert repo.append.call_args.kwargs["actor"] == "anonymous"
                        assert (
                            repo.append.call_args.kwargs["metadata"]["reason"] == "invalid_csrf"
                        )


@pytest.mark.unit
@pytest.mark.integration
def test_successful_login_retains_administrator_actor() -> None:
    store = FakeRateLimitStore()
    with shared_rate_limiter(store):
        with mock_db_connection():
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                with patch("app.admin_routes.db.create_admin_session", return_value=42):
                    with patch(
                        "app.admin_routes.audit_service.record_login_success"
                    ) as success_audit:
                        login = client.post(
                            "/admin/login",
                            data={
                                "username": TEST_USERNAME,
                                "password": TEST_PASSWORD,
                                "csrf_token": "flow-csrf",
                            },
                        )
                        assert login.status_code == 303
                        success_audit.assert_called_once()
                        assert (
                            success_audit.call_args.kwargs["actor_context"].actor
                            == TEST_USERNAME
                        )
                        assert success_audit.call_args.kwargs["session_id"] == 42


@pytest.mark.unit
def test_failed_login_logs_do_not_leak_candidates_or_secrets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    store = FakeRateLimitStore()
    with shared_rate_limiter(store):
        with mock_db_connection():
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                with patch(
                    "app.admin_routes.audit_service.record_login_failure",
                    side_effect=RuntimeError("audit down"),
                ):
                    client.post(
                        "/admin/login",
                        data={
                            "username": ATTACKER_USERNAME,
                            "password": "wrong-password",
                            "csrf_token": "flow-csrf",
                        },
                    )
    combined = caplog.text + "".join(str(record.msg) for record in caplog.records)
    assert ATTACKER_USERNAME not in combined
    assert TEST_LIMITER_SECRET not in combined
    assert "203.0.113" not in combined


def _require_database_url() -> str:
    if _DATABASE_URL:
        return _DATABASE_URL
    if _REQUIRED:
        pytest.fail("REQUIRE_TEST_DATABASE=1 but TEST_DATABASE_URL is unset")
    pytest.skip("TEST_DATABASE_URL not set; skipping live Postgres limiter tests")


@pytest.fixture
def pg_conn() -> Generator[psycopg.Connection, None, None]:
    database_url = _require_database_url()
    with psycopg.connect(database_url, autocommit=False) as bootstrap:
        bootstrap.execute("DROP SCHEMA IF EXISTS public CASCADE")
        bootstrap.execute("CREATE SCHEMA public")
        bootstrap.execute("GRANT ALL ON SCHEMA public TO CURRENT_USER")
        bootstrap.execute("GRANT ALL ON SCHEMA public TO public")
        apply_migrations(bootstrap)
        bootstrap.commit()
    conn = psycopg.connect(database_url, row_factory=dict_row, autocommit=False)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()
        with psycopg.connect(database_url, autocommit=False) as cleanup:
            cleanup.execute("DROP SCHEMA IF EXISTS public CASCADE")
            cleanup.execute("CREATE SCHEMA public")
            cleanup.commit()


@pytest.mark.integration
def test_postgres_persists_hmac_limiter_keys_and_anonymous_failure_actor(
    pg_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", _DATABASE_URL)
    settings = get_settings()
    source_key = admin_auth.build_source_rate_limit_key("203.0.113.200", settings)
    plain = _plain_sha256_limiter_key("src", "203.0.113.200")
    assert source_key != plain

    now = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    db.try_admit_admin_login(
        pg_conn,
        limiter_keys=(source_key,),
        now=now,
        rate_limit=5,
        window_seconds=900,
        lockout_seconds=900,
    )
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT limiter_key FROM admin_login_rate_limits WHERE limiter_key = %s",
            (source_key,),
        )
        row = cur.fetchone()
    assert row is not None
    assert row["limiter_key"] == source_key
    assert len(row["limiter_key"]) == LIMITER_KEY_HEX_LENGTH

    audit_repo = PostgresAuditEventRepository()
    with crm_transaction(pg_conn):
        audit_service.record_login_failure(
            pg_conn,
            actor_context=MagicMock(actor="anonymous", correlation_id="corr-pg"),
            reason="invalid_credentials",
            repository=audit_repo,
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
    pg_conn.commit()

    assert audit_row is not None
    assert audit_row["actor"] == "anonymous"
    assert audit_row["summary_after"] == {"reason": "invalid_credentials"}
    assert audit_row["metadata"] == {"reason": "invalid_credentials"}
    serialized = json.dumps(audit_row)
    assert ATTACKER_USERNAME not in serialized
    assert TEST_USERNAME not in serialized
