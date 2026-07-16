"""Tests for keyed admin login limiter identifiers and anonymous failure actors."""

from __future__ import annotations

import hashlib
import logging
import os
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Generator
from unittest.mock import MagicMock, patch

import psycopg
import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from app import admin_auth, audit_service, db
from app.admin_auth import LOGIN_FLOW_COOKIE_NAME, SESSION_COOKIE_NAME
from app.actor_context import ActorContext
from app.admin_security import validate_admin_security_config, validate_admin_secret
from app.config import Settings, get_settings
from app.main import app
from app.migrations.runner import apply_migrations

client = TestClient(app, follow_redirects=False)

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "test-session-secret-32chars-minimum"
TEST_LIMITER_SECRET = "test-limiter-secret-32chars-minimum"
ALT_LIMITER_SECRET = "alt-limiter-secret-32chars-minimum!"
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
    monkeypatch.delenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", raising=False)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    admin_auth.reset_login_rate_limiter()


def _plain_sha256_digest(domain: str, material: str) -> str:
    return hashlib.sha256(f"{domain}:{material}".encode("utf-8")).hexdigest()


def _settings(**overrides: str) -> Settings:
    return get_settings()


@contextmanager
def mock_db_connection() -> Generator[MagicMock, None, None]:
    conn = MagicMock()
    with ExitStack() as stack:
        db_conn_patch = stack.enter_context(patch("app.admin_routes.db.db_connection"))
        stack.enter_context(
            patch("app.admin_routes.db.create_admin_login_flow", return_value=1)
        )
        stack.enter_context(
            patch("app.admin_routes.db.claim_admin_login_flow", return_value={"id": 1})
        )
        stack.enter_context(
            patch("app.admin_routes.db.create_admin_session", return_value=42)
        )
        db_conn_patch.return_value.__enter__.return_value = conn
        db_conn_patch.return_value.__exit__.return_value = None
        yield conn


@pytest.mark.unit
def test_limiter_identifier_is_not_plain_sha256() -> None:
    settings = get_settings()
    source = "203.0.113.10"
    identifier = admin_auth.build_source_rate_limit_key(source, settings)
    assert identifier != _plain_sha256_digest("src", source.strip().lower())
    assert len(identifier) == 64


@pytest.mark.unit
def test_limiter_identifier_depends_on_secret() -> None:
    base = get_settings()
    other = Settings(
        database_url=base.database_url,
        stripe_secret_key=base.stripe_secret_key,
        stripe_webhook_secret=base.stripe_webhook_secret,
        stripe_publishable_key=base.stripe_publishable_key,
        resend_api_key=base.resend_api_key,
        from_email=base.from_email,
        notify_email=base.notify_email,
        base_url=base.base_url,
        plausible_domain=base.plausible_domain,
        plausible_api_key=base.plausible_api_key,
        analytics_environment=base.analytics_environment,
        admin_username=base.admin_username,
        admin_password_hash=base.admin_password_hash,
        admin_session_secret=base.admin_session_secret,
        admin_login_limiter_secret=ALT_LIMITER_SECRET,
    )
    material = "203.0.113.10"
    current = admin_auth.build_source_rate_limit_key(material, base)
    alternate = admin_auth.build_source_rate_limit_key(material, other)
    assert current != alternate


@pytest.mark.unit
def test_limiter_identifier_is_stable_across_calls() -> None:
    settings = get_settings()
    first = admin_auth.build_source_rate_limit_key("203.0.113.10", settings)
    second = admin_auth.build_source_rate_limit_key("203.0.113.10", settings)
    assert first == second


@pytest.mark.unit
def test_limiter_domain_separation() -> None:
    settings = get_settings()
    shared_material = "203.0.113.10"
    source_key = admin_auth.digest_limiter_key(
        secret=settings.admin_login_limiter_secret,
        domain="src",
        material=shared_material,
    )
    account_key = admin_auth.digest_limiter_key(
        secret=settings.admin_login_limiter_secret,
        domain="acct",
        material=shared_material,
    )
    assert source_key != account_key


@pytest.mark.unit
@pytest.mark.parametrize(
    ("env_name", "value"),
    [
        ("ADMIN_LOGIN_LIMITER_SECRET", ""),
        ("ADMIN_LOGIN_LIMITER_SECRET", "short"),
        ("ADMIN_LOGIN_LIMITER_SECRET", "changeme" * 4),
        ("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", "a" * 32),
    ],
)
def test_limiter_secret_validation_rejects_weak_material(
    monkeypatch: pytest.MonkeyPatch,
    env_name: str,
    value: str,
) -> None:
    if env_name == "ADMIN_LOGIN_LIMITER_SECRET":
        monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", value)
    else:
        monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
        monkeypatch.setenv(env_name, value)
    settings = get_settings()
    with pytest.raises(ValueError):
        validate_admin_security_config(settings)


@pytest.mark.unit
def test_limiter_secret_validation_rejects_matching_previous_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", TEST_LIMITER_SECRET)
    settings = get_settings()
    with pytest.raises(ValueError, match="must differ"):
        validate_admin_security_config(settings)


@pytest.mark.unit
def test_rotation_checks_previous_lockout_but_writes_current_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", PREVIOUS_LIMITER_SECRET)
    settings = get_settings()
    previous_source_key = admin_auth.digest_limiter_key(
        secret=PREVIOUS_LIMITER_SECRET,
        domain="src",
        material="testclient",
    )
    current_source_key = admin_auth.build_source_rate_limit_key("testclient", settings)
    assert previous_source_key != current_source_key

    store_rows: dict[str, dict[str, Any]] = {
        previous_source_key: {
            "failure_count": 5,
            "window_started_at": datetime.now(timezone.utc),
            "locked_until": datetime.now(timezone.utc) + timedelta(seconds=900),
            "updated_at": datetime.now(timezone.utc),
        }
    }

    def is_throttled(conn: Any, *, limiter_key: str, now: datetime) -> bool:
        row = store_rows.get(limiter_key)
        if row is None:
            return False
        locked_until = row.get("locked_until")
        return locked_until is not None and locked_until > now

    def try_admit(
        conn: Any,
        *,
        limiter_keys: tuple[str, ...],
        now: datetime,
        rate_limit: int,
        window_seconds: int,
        lockout_seconds: int,
    ) -> db.AdminLoginAdmission:
        for key in limiter_keys:
            store_rows.setdefault(
                key,
                {
                    "failure_count": 0,
                    "window_started_at": now,
                    "locked_until": None,
                    "updated_at": now,
                },
            )
            store_rows[key]["failure_count"] += 1
        return db.AdminLoginAdmission(
            admitted=True,
            throttled=False,
            already_locked=False,
            lockout_transition=False,
        )

    scope = {
        "type": "http",
        "headers": [],
        "client": ("testclient", 12345),
        "method": "POST",
        "path": "/admin/login",
    }
    from starlette.requests import Request

    request = Request(scope)
    with (
        patch("app.admin_auth.db.db_connection") as db_conn,
        patch("app.admin_auth.db.is_admin_login_throttled", side_effect=is_throttled),
        patch("app.admin_auth.db.try_admit_admin_login", side_effect=try_admit),
        patch(
            "app.admin_auth.db.cleanup_expired_admin_login_rate_limits",
            return_value=0,
        ),
    ):
        db_conn.return_value.__enter__.return_value = MagicMock()
        db_conn.return_value.__exit__.return_value = None
        blocked = admin_auth.try_admit_login_attempt(
            request, settings, username=TEST_USERNAME
        )
    assert not blocked.admitted
    assert blocked.throttled
    assert current_source_key not in store_rows or store_rows[current_source_key]["failure_count"] == 0


@pytest.mark.unit
@pytest.mark.integration
def test_unknown_username_failure_audit_uses_anonymous_actor() -> None:
    repo = MagicMock()
    repo.append.return_value = {"id": "evt-1"}
    admitted = admin_auth.LoginAdmissionResult(
        admitted=True,
        throttled=False,
        already_locked=False,
        lockout_transition=False,
    )
    with mock_db_connection() as conn:
        with patch(
            "app.admin_routes.admin_auth.try_admit_login_attempt",
            return_value=admitted,
        ):
            with patch(
                "app.admin_routes.audit_service.record_login_failure",
                wraps=audit_service.record_login_failure,
            ) as failure_audit:
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
    failure_audit.assert_called_once()
    actor_context = failure_audit.call_args.kwargs["actor_context"]
    assert actor_context.actor == "anonymous"
    assert "ghost-attacker" not in repr(failure_audit.call_args.kwargs)


@pytest.mark.unit
@pytest.mark.integration
def test_configured_username_wrong_password_audit_uses_anonymous_actor() -> None:
    admitted = admin_auth.LoginAdmissionResult(
        admitted=True,
        throttled=False,
        already_locked=False,
        lockout_transition=False,
    )
    with mock_db_connection():
        with patch(
            "app.admin_routes.admin_auth.try_admit_login_attempt",
            return_value=admitted,
        ):
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                with patch(
                    "app.admin_routes.audit_service.record_login_failure"
                ) as failure_audit:
                    response = client.post(
                        "/admin/login",
                        data={
                            "username": TEST_USERNAME,
                            "password": "wrong-password",
                            "csrf_token": "flow-csrf",
                        },
                    )
    assert response.status_code == 401
    failure_audit.assert_called_once()
    assert failure_audit.call_args.kwargs["actor_context"].actor == "anonymous"
    assert TEST_USERNAME not in repr(failure_audit.call_args.kwargs)


@pytest.mark.unit
@pytest.mark.integration
def test_invalid_csrf_failure_audit_uses_anonymous_actor() -> None:
    admitted = admin_auth.LoginAdmissionResult(
        admitted=True,
        throttled=False,
        already_locked=False,
        lockout_transition=False,
    )
    with mock_db_connection():
        with patch(
            "app.admin_routes.admin_auth.try_admit_login_attempt",
            return_value=admitted,
        ):
            with patch("app.admin_routes._try_claim_login_flow", return_value=False):
                with patch(
                    "app.admin_routes.audit_service.record_login_failure"
                ) as failure_audit:
                    response = client.post(
                        "/admin/login",
                        data={
                            "username": "csrf-attacker",
                            "password": "wrong-password",
                            "csrf_token": "flow-csrf",
                        },
                    )
    assert response.status_code == 400
    failure_audit.assert_called_once()
    assert failure_audit.call_args.kwargs["actor_context"].actor == "anonymous"
    assert failure_audit.call_args.kwargs["reason"] == "invalid_csrf"


@pytest.mark.unit
@pytest.mark.integration
def test_successful_login_audit_retains_administrator_actor() -> None:
    admitted = admin_auth.LoginAdmissionResult(
        admitted=True,
        throttled=False,
        already_locked=False,
        lockout_transition=False,
    )
    with mock_db_connection():
        with patch(
            "app.admin_routes.admin_auth.try_admit_login_attempt",
            return_value=admitted,
        ):
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
def test_login_failure_logs_exclude_candidate_and_secret_material(
    caplog: pytest.LogCaptureFixture,
) -> None:
    repo = MagicMock()
    repo.append.return_value = {"id": "evt-log"}
    candidate = "leaked-candidate-user"
    with caplog.at_level(logging.ERROR):
        with mock_db_connection():
            with patch(
                "app.admin_routes.admin_auth.try_admit_login_attempt",
                return_value=admin_auth.LoginAdmissionResult(
                    admitted=True,
                    throttled=False,
                    already_locked=False,
                    lockout_transition=False,
                ),
            ):
                with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                    with patch(
                        "app.admin_routes.audit_service.record_login_failure",
                        side_effect=RuntimeError("audit store failure"),
                    ):
                        client.post(
                        "/admin/login",
                        data={
                            "username": candidate,
                            "password": "wrong-password",
                            "csrf_token": "flow-csrf",
                        },
                    )
    combined = caplog.text
    for forbidden in (
        candidate,
        TEST_LIMITER_SECRET,
        TEST_SECRET,
        "203.0.113.",
        "src:",
        "acct:",
    ):
        assert forbidden not in combined


def _require_database_url() -> str:
    if _DATABASE_URL:
        return _DATABASE_URL
    if _REQUIRED:
        pytest.fail("REQUIRE_TEST_DATABASE=1 but TEST_DATABASE_URL is unset")
    pytest.skip("TEST_DATABASE_URL not set; skipping live Postgres limiter tests")


@pytest.mark.integration
def test_postgres_persists_keyed_limiter_identifiers_and_anonymous_actors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _require_database_url()
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    settings = get_settings()
    source_key = admin_auth.build_source_rate_limit_key("203.0.113.88", settings)
    now = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)

    with psycopg.connect(database_url, autocommit=False) as bootstrap:
        bootstrap.execute("DROP SCHEMA IF EXISTS public CASCADE")
        bootstrap.execute("CREATE SCHEMA public")
        bootstrap.commit()
        apply_migrations(bootstrap)

    with psycopg.connect(database_url, row_factory=dict_row, autocommit=False) as conn:
        admission = db.try_admit_admin_login(
            conn,
            limiter_keys=(source_key,),
            now=now,
            rate_limit=5,
            window_seconds=900,
            lockout_seconds=900,
        )
        assert admission.admitted
        with conn.cursor() as cur:
            cur.execute(
                "SELECT limiter_key FROM admin_login_rate_limits WHERE limiter_key = %s",
                (source_key,),
            )
            row = cur.fetchone()
        assert row is not None
        assert row["limiter_key"] == source_key
        assert row["limiter_key"] != _plain_sha256_digest("src", "203.0.113.88")
        assert len(row["limiter_key"]) == 64

    repo = MagicMock()
    repo.append.return_value = {"id": "evt-pg"}
    with mock_db_connection() as audit_conn:
        audit_service.record_login_failure(
            audit_conn,
            actor_context=ActorContext(actor="anonymous", correlation_id="trace-1"),
            reason="invalid_credentials",
            repository=repo,
        )
    assert repo.append.call_args.kwargs["actor_context"].actor == "anonymous"

    with psycopg.connect(database_url, autocommit=False) as cleanup:
        cleanup.execute("DROP SCHEMA IF EXISTS public CASCADE")


@pytest.mark.unit
def test_validate_admin_secret_accepts_strong_values() -> None:
    validate_admin_secret("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)


@pytest.mark.unit
def test_rotation_cleanup_eligible_for_previous_key_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", PREVIOUS_LIMITER_SECRET)
    settings = get_settings()
    stale_key = admin_auth.digest_limiter_key(
        secret=PREVIOUS_LIMITER_SECRET,
        domain="src",
        material="203.0.113.66",
    )
    now = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cur.rowcount = 1
    deleted = db.cleanup_expired_admin_login_rate_limits(
        conn,
        now=now + timedelta(seconds=200),
        window_seconds=60,
        lockout_seconds=60,
    )
    assert deleted == 1
    assert stale_key != admin_auth.build_source_rate_limit_key("203.0.113.66", settings)
