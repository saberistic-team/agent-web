"""Tests for keyed admin login limiter identifiers and anonymous failure actors."""

from __future__ import annotations

import hashlib
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

from app import admin_auth, audit_service, db
from app.actor_context import ActorContext
from app.config import Settings, get_settings
from app.crm_uow import crm_transaction
from app.main import app
from app.migrations.runner import apply_migrations
from app.repositories.postgres import PostgresAuditEventRepository

from tests.conftest import TEST_LIMITER_SECRET

client = TestClient(app, follow_redirects=False)

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SESSION_SECRET = "test-session-secret-32chars-minimum"
ALT_LIMITER_SECRET = "limiter-secret-bravo-32chars-minimum!"
PREVIOUS_LIMITER_SECRET = "limiter-secret-charlie-32chars-min!"


def _settings(**overrides: str) -> Settings:
    base = {
        "database_url": "postgresql://test:test@localhost:5432/test",
        "stripe_secret_key": "",
        "stripe_webhook_secret": "",
        "stripe_publishable_key": "",
        "resend_api_key": "",
        "from_email": "noreply@saberistic.com",
        "notify_email": "inbox@saberistic.com",
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
    base.update(overrides)
    return Settings(**base)


def _plain_sha256_limiter_key(prefix: str, material: str) -> str:
    payload = f"{prefix}:{material}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@pytest.mark.unit
def test_limiter_identifier_is_not_plain_sha256() -> None:
    settings = _settings()
    source = "203.0.113.10"
    account = "operator"
    source_key = admin_auth.build_source_rate_limit_key(source, settings=settings)
    account_key = admin_auth.build_account_rate_limit_key(account, settings=settings)
    assert source_key != _plain_sha256_limiter_key("src", source)
    assert account_key != _plain_sha256_limiter_key("acct", account)
    assert len(source_key) == 64
    assert len(account_key) == 64


@pytest.mark.unit
def test_limiter_identifier_depends_on_secret() -> None:
    settings_a = _settings(admin_login_limiter_secret=TEST_LIMITER_SECRET)
    settings_b = _settings(admin_login_limiter_secret=ALT_LIMITER_SECRET)
    source = "203.0.113.10"
    key_a = admin_auth.build_source_rate_limit_key(source, settings=settings_a)
    key_b = admin_auth.build_source_rate_limit_key(source, settings=settings_b)
    assert key_a != key_b


@pytest.mark.unit
def test_limiter_identifier_is_stable_across_calls() -> None:
    settings = _settings()
    source = "203.0.113.10"
    first = admin_auth.build_source_rate_limit_key(source, settings=settings)
    second = admin_auth.build_source_rate_limit_key(source, settings=settings)
    assert first == second


@pytest.mark.unit
def test_limiter_identifier_domain_separation() -> None:
    settings = _settings()
    shared_material = "203.0.113.10"
    source_key = admin_auth.build_source_rate_limit_key(shared_material, settings=settings)
    account_key = admin_auth.build_account_rate_limit_key(shared_material, settings=settings)
    assert source_key != account_key


@pytest.mark.unit
@pytest.mark.parametrize(
    ("secret", "env_name"),
    [
        ("", "ADMIN_LOGIN_LIMITER_SECRET"),
        ("short-secret", "ADMIN_LOGIN_LIMITER_SECRET"),
        ("changeme" + "x" * 24, "ADMIN_LOGIN_LIMITER_SECRET"),
    ],
)
def test_limiter_secret_validation_rejects_weak_material(secret: str, env_name: str) -> None:
    with pytest.raises(ValueError, match=env_name):
        admin_auth._validate_limiter_secret_value(secret, env_name=env_name)


@pytest.mark.unit
def test_validate_admin_login_limiter_secrets_requires_database_and_username() -> None:
    admin_auth.validate_admin_login_limiter_secrets(_settings(database_url=""))
    admin_auth.validate_admin_login_limiter_secrets(_settings(admin_username=""))


@pytest.mark.unit
def test_validate_admin_login_limiter_secrets_checks_previous_key() -> None:
    with pytest.raises(ValueError, match="ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS"):
        admin_auth.validate_admin_login_limiter_secrets(
            _settings(admin_login_limiter_secret_previous="changeme" + "x" * 24)
        )


@pytest.mark.unit
def test_rotation_previous_secret_changes_identifier() -> None:
    settings = _settings(
        admin_login_limiter_secret=TEST_LIMITER_SECRET,
        admin_login_limiter_secret_previous=PREVIOUS_LIMITER_SECRET,
    )
    current = admin_auth.build_source_rate_limit_key("203.0.113.10", settings=settings)
    previous = admin_auth._digest_limiter_key(
        admin_auth.LIMITER_KEY_DOMAIN_SOURCE,
        "203.0.113.10",
        PREVIOUS_LIMITER_SECRET,
    )
    assert current != previous


@pytest.mark.unit
def test_rotation_lookup_keys_include_previous_secret_buckets() -> None:
    settings = _settings(
        admin_login_limiter_secret=TEST_LIMITER_SECRET,
        admin_login_limiter_secret_previous=PREVIOUS_LIMITER_SECRET,
    )
    lookup_keys = admin_auth._limiter_lookup_keys(
        settings=settings,
        submitted_username=TEST_USERNAME,
        client_source="203.0.113.10",
    )
    write_keys = admin_auth.login_limiter_keys(
        settings=settings,
        submitted_username=TEST_USERNAME,
        client_source="203.0.113.10",
    )
    assert len(lookup_keys) == 4
    assert len(write_keys) == 2
    assert set(write_keys).issubset(set(lookup_keys))


@pytest.fixture
def rate_limit_store() -> Any:
    from tests.test_admin_auth import FakeRateLimitStore

    return FakeRateLimitStore()


@contextmanager
def shared_rate_limiter(store: Any) -> Generator[None, None, None]:
    from tests.test_admin_auth import shared_rate_limiter as _shared

    with _shared(store):
        yield


@pytest.fixture(autouse=True)
def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SESSION_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    admin_auth.reset_login_rate_limiter()


def _fetch_login_form() -> tuple[str, dict[str, str]]:
    from tests.test_admin_auth import _fetch_login_form as fetch

    return fetch()


def _login(**data: str) -> Any:
    from tests.test_admin_auth import _login as login

    return login(**data)


@pytest.mark.unit
@pytest.mark.integration
def test_unknown_username_failure_records_anonymous_actor() -> None:
    repo = MagicMock()
    repo.append.return_value = {"id": "evt-1"}
    with patch("app.admin_routes.db.db_connection") as db_conn:
        conn = MagicMock()
        db_conn.return_value.__enter__.return_value = conn
        mock_repos = MagicMock()
        mock_repos.audit_events = repo
        with patch("app.admin_routes._try_claim_login_flow", return_value=True):
            with patch(
                "app.admin_routes.audit_service.record_login_failure",
                wraps=audit_service.record_login_failure,
            ) as failure_audit:
                with patch(
                    "app.admin_routes.audit_service.get_repositories",
                    return_value=mock_repos,
                ):
                    response = client.post(
                        "/admin/login",
                        data={
                            "username": "attacker-candidate",
                            "password": "wrong-password",
                            "csrf_token": "flow-csrf",
                        },
                    )
                    assert response.status_code == 401
                    failure_audit.assert_called_once()
                    actor_context = failure_audit.call_args.kwargs["actor_context"]
                    assert actor_context.actor == "anonymous"
                    append_kwargs = repo.append.call_args.kwargs
                    assert append_kwargs["actor"] == "anonymous"
                    serialized = str(append_kwargs)
                    assert "attacker-candidate" not in serialized


@pytest.mark.unit
@pytest.mark.integration
def test_configured_username_wrong_password_actor_remains_anonymous() -> None:
    repo = MagicMock()
    repo.append.return_value = {"id": "evt-2"}
    with patch("app.admin_routes.db.db_connection") as db_conn:
        conn = MagicMock()
        db_conn.return_value.__enter__.return_value = conn
        mock_repos = MagicMock()
        mock_repos.audit_events = repo
        with patch("app.admin_routes._try_claim_login_flow", return_value=True):
            with patch(
                "app.admin_routes.audit_service.record_login_failure",
                wraps=audit_service.record_login_failure,
            ) as failure_audit:
                with patch(
                    "app.admin_routes.audit_service.get_repositories",
                    return_value=mock_repos,
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
                    actor_context = failure_audit.call_args.kwargs["actor_context"]
                    assert actor_context.actor == "anonymous"
                    assert repo.append.call_args.kwargs["actor"] == "anonymous"


@pytest.mark.unit
@pytest.mark.integration
def test_invalid_flow_failure_actor_is_anonymous(
    rate_limit_store: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = MagicMock()
    repo.append.return_value = {"id": "evt-3"}
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "5")
    with shared_rate_limiter(rate_limit_store):
        with patch("app.admin_routes.db.db_connection") as db_conn:
            conn = MagicMock()
            db_conn.return_value.__enter__.return_value = conn
            with patch("app.admin_routes._try_claim_login_flow", return_value=False):
                mock_repos = MagicMock()
                mock_repos.audit_events = repo
                with patch(
                    "app.admin_routes.audit_service.record_login_failure",
                    wraps=audit_service.record_login_failure,
                ) as failure_audit:
                    with patch(
                        "app.admin_routes.audit_service.get_repositories",
                        return_value=mock_repos,
                    ):
                        response = client.post(
                            "/admin/login",
                            data={
                                "username": "flow-attacker",
                                "password": "wrong-password",
                                "csrf_token": "flow-csrf",
                            },
                        )
                        assert response.status_code == 400
                        failure_audit.assert_called_once()
                        assert (
                            failure_audit.call_args.kwargs["actor_context"].actor
                            == "anonymous"
                        )
                        assert failure_audit.call_args.kwargs["reason"] == "invalid_csrf"


@pytest.mark.unit
@pytest.mark.integration
def test_lockout_transition_audit_actor_is_anonymous(
    rate_limit_store: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = MagicMock()
    repo.append.return_value = {"id": "evt-4"}
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    with shared_rate_limiter(rate_limit_store):
        with patch("app.admin_routes.db.db_connection") as db_conn:
            conn = MagicMock()
            db_conn.return_value.__enter__.return_value = conn
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                mock_repos = MagicMock()
                mock_repos.audit_events = repo
                with patch(
                    "app.admin_routes.audit_service.record_login_failure",
                    wraps=audit_service.record_login_failure,
                ) as failure_audit:
                    with patch(
                        "app.admin_routes.audit_service.get_repositories",
                        return_value=mock_repos,
                    ):
                        assert _login(password="wrong").status_code == 401
                        lockout = _login(password="wrong")
                        assert lockout.status_code == 401
                        assert failure_audit.call_args.kwargs["reason"] == "rate_limited"
                        assert (
                            failure_audit.call_args.kwargs["actor_context"].actor
                            == "anonymous"
                        )


@pytest.mark.unit
@pytest.mark.integration
def test_successful_login_retains_authenticated_actor() -> None:
    with patch("app.admin_routes.db.db_connection") as db_conn:
        conn = MagicMock()
        db_conn.return_value.__enter__.return_value = conn
        with patch("app.admin_routes._try_claim_login_flow", return_value=True):
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
def test_login_failure_logs_exclude_candidate_and_secret(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    with patch("app.admin_routes.db.db_connection") as db_conn:
        conn = MagicMock()
        db_conn.return_value.__enter__.return_value = conn
        with patch("app.admin_routes._try_claim_login_flow", return_value=True):
            with patch(
                "app.admin_routes.audit_service.record_login_failure",
                return_value=None,
            ):
                response = client.post(
                    "/admin/login",
                    data={
                        "username": "log-candidate-user",
                        "password": "wrong-password",
                        "csrf_token": "flow-csrf",
                    },
                )
                assert response.status_code == 401
    combined = caplog.text + str(response.text)
    assert "log-candidate-user" not in combined
    assert TEST_LIMITER_SECRET not in combined
    assert "src:" not in combined
    assert "acct:" not in combined


_REQUIRED = (os.environ.get("REQUIRE_TEST_DATABASE") or "").strip() in {"1", "true", "yes"}
_DATABASE_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip()


def _require_database_url() -> str:
    if _DATABASE_URL:
        return _DATABASE_URL
    if _REQUIRED:
        pytest.fail("REQUIRE_TEST_DATABASE=1 but TEST_DATABASE_URL is unset")
    pytest.skip("TEST_DATABASE_URL not set; skipping live Postgres login limiter tests")


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
    conn = psycopg.connect(database_url, autocommit=False)
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
def test_postgres_persists_hmac_limiter_key_and_anonymous_actor(pg_conn: psycopg.Connection) -> None:
    settings = _settings()
    source_key = admin_auth.build_source_rate_limit_key("203.0.113.88", settings=settings)
    plain = _plain_sha256_limiter_key("src", "203.0.113.88")
    now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    admission = db.try_admit_admin_login(
        pg_conn,
        limiter_keys=(source_key,),
        now=now,
        rate_limit=5,
        window_seconds=900,
        lockout_seconds=900,
    )
    assert admission.admitted

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT limiter_key FROM admin_login_rate_limits WHERE limiter_key = %s",
            (source_key,),
        )
        row = cur.fetchone()
    assert row is not None
    assert row[0] == source_key
    assert row[0] != plain

    repo = PostgresAuditEventRepository()
    actor = ActorContext(actor="anonymous", correlation_id="corr-pg-242")
    with crm_transaction(pg_conn):
        audit_service.record_login_failure(
            pg_conn,
            actor_context=actor,
            reason="invalid_credentials",
            repository=repo,
        )
    pg_conn.commit()

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
    assert audit_row[0] == "anonymous"
    assert "ghost" not in str(audit_row[1])
    assert "ghost" not in str(audit_row[2])


@pytest.mark.integration
def test_rotation_cleanup_removes_previous_secret_rows(pg_conn: psycopg.Connection) -> None:
    previous_key = admin_auth._digest_limiter_key(
        admin_auth.LIMITER_KEY_DOMAIN_SOURCE,
        "203.0.113.99",
        PREVIOUS_LIMITER_SECRET,
    )
    now = datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc)
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO admin_login_rate_limits (
                limiter_key, failure_count, window_started_at, locked_until, updated_at
            )
            VALUES (%s, 1, %s, NULL, %s)
            """,
            (previous_key, now - timedelta(hours=2), now - timedelta(hours=2)),
        )
    pg_conn.commit()

    deleted = db.cleanup_expired_admin_login_rate_limits(
        pg_conn,
        now=now,
        window_seconds=60,
        lockout_seconds=60,
    )
    assert deleted >= 1
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM admin_login_rate_limits WHERE limiter_key = %s",
            (previous_key,),
        )
        count = cur.fetchone()
    assert count is not None
    assert count[0] == 0
