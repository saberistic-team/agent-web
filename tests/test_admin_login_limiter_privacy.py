"""Tests for keyed admin login limiter identifiers and anonymous failure actors."""

from __future__ import annotations

import hashlib
import json
import logging
import os
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
from app.actor_context import anonymous_actor_context
from app.config import Settings
from app.crm_uow import crm_transaction
from app.main import app
from app.migrations.runner import apply_migrations
from app.repositories.postgres import get_repositories
from tests.conftest import TEST_LIMITER_SECRET

client = TestClient(app, follow_redirects=False)

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "test-session-secret-32chars-minimum"
ALT_LIMITER_SECRET = "alt-limiter-secret-32chars-minimum-!!"
PREVIOUS_LIMITER_SECRET = "prev-limiter-secret-32chars-minimum-!"

_REQUIRED = (os.environ.get("REQUIRE_TEST_DATABASE") or "").strip() in {"1", "true", "yes"}
_DATABASE_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip()


def _plain_sha256_identifier(prefix: str, material: str) -> str:
    payload = f"{prefix}:{material}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _settings(**overrides: str) -> Settings:
    base = {
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
        "admin_session_secret": TEST_SECRET,
        "admin_login_limiter_secret": TEST_LIMITER_SECRET,
        "admin_login_limiter_secret_previous": "",
    }
    base.update(overrides)
    return Settings(**base)


@pytest.fixture(autouse=True)
def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    admin_auth.reset_login_rate_limiter()


class AuditSpyRepository:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def append(self, conn: Any, **kwargs: Any) -> dict[str, Any]:
        self.events.append(kwargs)
        return {"id": f"evt-{len(self.events)}"}

    def list_page(self, conn: Any, *, page: int, per_page: int) -> dict[str, Any]:
        return {"items": [], "total": 0, "page": page, "per_page": per_page}


@contextmanager
def audit_spy() -> Generator[AuditSpyRepository, None, None]:
    repo = AuditSpyRepository()
    repos = MagicMock()
    repos.audit_events = repo
    with patch("app.audit_service.get_repositories", return_value=repos):
        yield repo


@contextmanager
def admitted_login_attempt() -> Generator[None, None, None]:
    admitted = admin_auth.LoginAdmissionResult(
        admitted=True,
        throttled=False,
        already_locked=False,
        lockout_transition=False,
    )
    with patch("app.admin_routes.admin_auth.try_admit_login_attempt", return_value=admitted):
        yield


@contextmanager
def mock_db_connection() -> Generator[MagicMock, None, None]:
    conn = MagicMock()
    with (
        patch("app.admin_routes.db.db_connection") as routes_conn,
        patch("app.admin_auth.db.db_connection") as auth_conn,
    ):
        routes_conn.return_value.__enter__.return_value = conn
        routes_conn.return_value.__exit__.return_value = None
        auth_conn.return_value.__enter__.return_value = conn
        auth_conn.return_value.__exit__.return_value = None
        yield conn


def _require_database_url() -> str:
    if _DATABASE_URL:
        return _DATABASE_URL
    if _REQUIRED:
        pytest.fail("REQUIRE_TEST_DATABASE=1 but TEST_DATABASE_URL is unset")
    pytest.skip("TEST_DATABASE_URL not set; skipping live Postgres limiter privacy tests")


@pytest.fixture
def pg_conn() -> Iterator[psycopg.Connection]:
    database_url = _require_database_url()
    with psycopg.connect(database_url, autocommit=False) as bootstrap:
        bootstrap.execute("DROP SCHEMA IF EXISTS public CASCADE")
        bootstrap.execute("CREATE SCHEMA public")
        bootstrap.execute("GRANT ALL ON SCHEMA public TO CURRENT_USER")
        bootstrap.execute("GRANT ALL ON SCHEMA public TO public")
        bootstrap.commit()
        apply_migrations(bootstrap)
    conn = psycopg.connect(database_url, row_factory=dict_row, autocommit=False)
    try:
        yield conn
    finally:
        conn.close()
        with psycopg.connect(database_url, autocommit=False) as cleanup:
            cleanup.execute("DROP SCHEMA IF EXISTS public CASCADE")
            cleanup.execute("CREATE SCHEMA public")
            cleanup.commit()


@pytest.mark.unit
def test_limiter_identifier_is_not_plain_sha256() -> None:
    settings = _settings()
    source_key = admin_auth.build_source_rate_limit_key("203.0.113.1", settings)
    account_key = admin_auth.build_account_rate_limit_key("operator", settings)
    assert source_key != _plain_sha256_identifier("src", "203.0.113.1")
    assert account_key != _plain_sha256_identifier("acct", "operator")


@pytest.mark.unit
def test_limiter_identifier_depends_on_secret() -> None:
    settings_a = _settings(admin_login_limiter_secret=TEST_LIMITER_SECRET)
    settings_b = _settings(admin_login_limiter_secret=ALT_LIMITER_SECRET)
    assert admin_auth.build_source_rate_limit_key("203.0.113.1", settings_a) != (
        admin_auth.build_source_rate_limit_key("203.0.113.1", settings_b)
    )


@pytest.mark.unit
def test_limiter_identifier_stable_across_calls() -> None:
    settings = _settings()
    first = admin_auth.build_source_rate_limit_key("203.0.113.1", settings)
    second = admin_auth.build_source_rate_limit_key("203.0.113.1", settings)
    assert first == second
    assert len(first) == 64


@pytest.mark.unit
def test_limiter_domain_separation() -> None:
    settings = _settings()
    shared_material = "203.0.113.1"
    source_key = admin_auth.build_source_rate_limit_key(shared_material, settings)
    account_key = admin_auth.build_account_rate_limit_key(shared_material, settings)
    assert source_key != account_key


@pytest.mark.unit
@pytest.mark.parametrize(
    ("secret", "env_name"),
    [
        ("", "ADMIN_LOGIN_LIMITER_SECRET"),
        ("short", "ADMIN_LOGIN_LIMITER_SECRET"),
        ("changeme-limiter-secret-32chars-minimum", "ADMIN_LOGIN_LIMITER_SECRET"),
    ],
)
def test_limiter_secret_validation_rejects_weak_material(
    secret: str,
    env_name: str,
) -> None:
    with pytest.raises(ValueError, match=env_name):
        admin_auth.validate_admin_login_limiter_secret(secret, env_name=env_name)


@pytest.mark.unit
def test_validate_admin_security_config_checks_previous_secret() -> None:
    settings = _settings(
        admin_login_limiter_secret=TEST_LIMITER_SECRET,
        admin_login_limiter_secret_previous="placeholder-previous-secret-value",
    )
    with pytest.raises(ValueError, match="ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS"):
        admin_auth.validate_admin_security_config(settings)


@pytest.mark.unit
def test_rotation_throttle_keys_include_previous_secret_variant() -> None:
    settings = _settings(
        admin_login_limiter_secret=TEST_LIMITER_SECRET,
        admin_login_limiter_secret_previous=PREVIOUS_LIMITER_SECRET,
    )
    throttle_keys = admin_auth.login_limiter_throttle_keys(
        submitted_username=TEST_USERNAME,
        client_source="203.0.113.1",
        configured_admin_username=TEST_USERNAME,
        settings=settings,
    )
    admission_keys = admin_auth.login_limiter_admission_keys(
        submitted_username=TEST_USERNAME,
        client_source="203.0.113.1",
        configured_admin_username=TEST_USERNAME,
        settings=settings,
    )
    previous_source = admin_auth._digest_limiter_key(
        "src", "203.0.113.1", PREVIOUS_LIMITER_SECRET
    )
    assert previous_source in throttle_keys
    assert previous_source not in admission_keys


@pytest.mark.integration
def test_rotation_previous_key_lockout_blocks_without_incrementing_current(
    pg_conn: psycopg.Connection,
) -> None:
    settings = _settings(
        admin_login_limiter_secret=TEST_LIMITER_SECRET,
        admin_login_limiter_secret_previous=PREVIOUS_LIMITER_SECRET,
    )
    now = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    previous_source = admin_auth._digest_limiter_key(
        "src", "203.0.113.9", PREVIOUS_LIMITER_SECRET
    )
    current_source = admin_auth.build_source_rate_limit_key("203.0.113.9", settings)
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO admin_login_rate_limits (
                limiter_key, failure_count, window_started_at, locked_until, updated_at
            )
            VALUES (%s, 5, %s, %s, %s)
            """,
            (
                previous_source,
                now,
                now + timedelta(minutes=15),
                now,
            ),
        )
    pg_conn.commit()

    admission = db.try_admit_admin_login(
        pg_conn,
        limiter_keys=(previous_source, current_source),
        increment_keys=(current_source,),
        now=now,
        rate_limit=5,
        window_seconds=900,
        lockout_seconds=900,
    )
    assert not admission.admitted
    assert admission.already_locked

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT limiter_key FROM admin_login_rate_limits WHERE limiter_key = %s",
            (current_source,),
        )
        assert cur.fetchone() is None

    deleted = db.cleanup_expired_admin_login_rate_limits(
        pg_conn,
        now=now + timedelta(hours=1),
        window_seconds=900,
        lockout_seconds=900,
    )
    assert deleted >= 1


@pytest.mark.unit
@pytest.mark.integration
def test_unknown_username_failure_records_anonymous_actor() -> None:
    with mock_db_connection(), audit_spy() as repo, admitted_login_attempt():
        with patch("app.admin_routes._try_claim_login_flow", return_value=True):
            response = client.post(
                "/admin/login",
                data={
                    "username": "ghost-attacker",
                    "password": "wrong-password",
                    "csrf_token": "flow-csrf",
                },
            )
            assert response.status_code == 401
            assert len(repo.events) == 1
            event = repo.events[0]
            assert event["actor"] == "anonymous"
            payload = json.dumps(event, default=str)
            assert "ghost-attacker" not in payload


@pytest.mark.unit
@pytest.mark.integration
def test_configured_username_wrong_password_actor_remains_anonymous() -> None:
    with mock_db_connection(), audit_spy() as repo, admitted_login_attempt():
        with patch("app.admin_routes._try_claim_login_flow", return_value=True):
            response = client.post(
                "/admin/login",
                data={
                    "username": TEST_USERNAME,
                    "password": "wrong-password",
                    "csrf_token": "flow-csrf",
                },
            )
            assert response.status_code == 401
            assert repo.events[0]["actor"] == "anonymous"
            assert TEST_USERNAME not in json.dumps(repo.events[0], default=str)


@pytest.mark.unit
@pytest.mark.integration
def test_invalid_csrf_failure_actor_is_anonymous() -> None:
    with mock_db_connection(), audit_spy() as repo, admitted_login_attempt():
        with patch("app.admin_routes._try_claim_login_flow", return_value=False):
            with patch("app.admin_routes._try_burn_login_flow_cookie", return_value=None):
                response = client.post(
                    "/admin/login",
                    data={
                        "username": "csrf-candidate",
                        "password": "wrong-password",
                        "csrf_token": "bad-csrf",
                    },
                )
                assert response.status_code == 400
                assert repo.events[0]["actor"] == "anonymous"
                assert repo.events[0]["summary_after"]["reason"] == "invalid_csrf"


@pytest.mark.unit
@pytest.mark.integration
def test_lockout_transition_audit_actor_is_anonymous() -> None:
    lockout_admission = admin_auth.LoginAdmissionResult(
        admitted=True,
        throttled=False,
        already_locked=False,
        lockout_transition=True,
    )
    with mock_db_connection(), audit_spy() as repo:
        with patch(
            "app.admin_routes.admin_auth.try_admit_login_attempt",
            return_value=lockout_admission,
        ):
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                response = client.post(
                    "/admin/login",
                    data={
                        "username": "lockout-candidate",
                        "password": "wrong-password",
                        "csrf_token": "flow-csrf",
                    },
                )
                assert response.status_code == 401
                assert repo.events[0]["actor"] == "anonymous"
                assert repo.events[0]["summary_after"]["reason"] == "rate_limited"


@pytest.mark.unit
@pytest.mark.integration
def test_successful_login_retains_administrator_actor() -> None:
    with mock_db_connection(), admitted_login_attempt():
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
                    assert success_audit.call_args.kwargs["actor_context"].actor == TEST_USERNAME
                    assert success_audit.call_args.kwargs["session_id"] == 42


@pytest.mark.unit
def test_login_failure_logs_exclude_candidates_and_secrets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    with mock_db_connection(), audit_spy(), admitted_login_attempt():
        with patch("app.admin_routes._try_claim_login_flow", return_value=True):
            response = client.post(
                "/admin/login",
                data={
                    "username": "logged-candidate",
                    "password": "wrong-password",
                    "csrf_token": "flow-csrf",
                },
            )
            assert response.status_code == 401
    combined = caplog.text + response.text
    for forbidden in (
        "logged-candidate",
        TEST_LIMITER_SECRET,
        "src:logged-candidate",
        "acct:logged-candidate",
    ):
        assert forbidden not in combined


@pytest.mark.integration
def test_postgres_persists_keyed_limiter_and_anonymous_failure_actor(
    pg_conn: psycopg.Connection,
) -> None:
    settings = _settings()
    source_key = admin_auth.build_source_rate_limit_key("203.0.113.200", settings)
    now = datetime(2026, 7, 2, 9, 0, tzinfo=timezone.utc)
    db.try_admit_admin_login(
        pg_conn,
        limiter_keys=(source_key,),
        now=now,
        rate_limit=5,
        window_seconds=900,
        lockout_seconds=900,
    )
    pg_conn.commit()

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT limiter_key, length(limiter_key) AS key_len FROM admin_login_rate_limits"
        )
        rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0]["limiter_key"] == source_key
    assert rows[0]["key_len"] == 64
    assert rows[0]["limiter_key"] != _plain_sha256_identifier("src", "203.0.113.200")

    with crm_transaction(pg_conn):
        audit_service.record_login_failure(
            pg_conn,
            actor_context=anonymous_actor_context(MagicMock(headers={})),
            reason="invalid_credentials",
            repository=get_repositories().audit_events,
        )
    pg_conn.commit()

    with pg_conn.cursor() as cur:
        cur.execute(
            """
            SELECT actor, summary_after
            FROM audit_events
            WHERE action = %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (audit_service.ACTION_AUTH_LOGIN_FAILURE,),
        )
        row = cur.fetchone()
    assert row is not None
    assert row["actor"] == "anonymous"
    assert row["summary_after"]["reason"] == "invalid_credentials"
    assert TEST_USERNAME not in json.dumps(row, default=str)
