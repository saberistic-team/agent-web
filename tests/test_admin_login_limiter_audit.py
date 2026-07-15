"""Tests for keyed login limiter identifiers and anonymous failure audit actors (#242)."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

import psycopg
import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from app import admin_auth, audit_service, db
from app.actor_context import ActorContext
from app.config import Settings, get_settings
from app.crm_uow import crm_transaction
from app.main import app
from app.migrations.runner import apply_migrations
from app.repositories.postgres import PostgresAuditEventRepository
from tests.test_admin_auth import (
    TEST_LIMITER_SECRET,
    TEST_PASSWORD,
    TEST_SECRET,
    TEST_USERNAME,
    mock_db_connection,
)

client = TestClient(app, follow_redirects=False)

TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
ALT_LIMITER_SECRET = "alternate-limiter-secret-32chars-min!!"
PREVIOUS_LIMITER_SECRET = "previous-limiter-secret-32chars-min!!"
CANDIDATE_USERNAME = "attacker-controlled-name"
CLIENT_SOURCE = "203.0.113.88"

_REQUIRED = (os.environ.get("REQUIRE_TEST_DATABASE") or "").strip() in {"1", "true", "yes"}
_DATABASE_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip()


def _require_database_url() -> str:
    if _DATABASE_URL:
        return _DATABASE_URL
    if _REQUIRED:
        pytest.fail("REQUIRE_TEST_DATABASE=1 but TEST_DATABASE_URL is unset")
    pytest.skip("TEST_DATABASE_URL not set; skipping live Postgres limiter audit tests")


def _settings_with_secrets(
    monkeypatch: pytest.MonkeyPatch,
    *,
    limiter_secret: str = TEST_LIMITER_SECRET,
    previous_secret: str = "",
) -> Settings:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", limiter_secret)
    if previous_secret:
        monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", previous_secret)
    else:
        monkeypatch.delenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", raising=False)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    return get_settings()


@pytest.fixture(autouse=True)
def limiter_audit_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings_with_secrets(monkeypatch)
    admin_auth.reset_login_rate_limiter()


@pytest.mark.unit
def test_limiter_identifier_is_not_plain_sha256() -> None:
    settings = get_settings()
    keyed = admin_auth.build_source_rate_limit_key(CLIENT_SOURCE, settings)
    plain = admin_auth.plain_sha256_limiter_key(
        admin_auth.LIMITER_DOMAIN_SOURCE,
        CLIENT_SOURCE.strip().lower(),
    )
    assert keyed != plain
    assert len(keyed) == 64


@pytest.mark.unit
def test_limiter_identifier_depends_on_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = _settings_with_secrets(monkeypatch, limiter_secret=TEST_LIMITER_SECRET)
    alternate = _settings_with_secrets(monkeypatch, limiter_secret=ALT_LIMITER_SECRET)
    key_a = admin_auth.build_source_rate_limit_key(CLIENT_SOURCE, current)
    key_b = admin_auth.build_source_rate_limit_key(CLIENT_SOURCE, alternate)
    assert key_a != key_b


@pytest.mark.unit
def test_limiter_identifier_stable_for_same_inputs() -> None:
    settings = get_settings()
    first = admin_auth.build_source_rate_limit_key(CLIENT_SOURCE, settings)
    second = admin_auth.build_source_rate_limit_key(CLIENT_SOURCE, settings)
    assert first == second


@pytest.mark.unit
def test_limiter_domain_separation() -> None:
    settings = get_settings()
    source_key = admin_auth.build_source_rate_limit_key("operator", settings)
    account_key = admin_auth.build_account_rate_limit_key("operator", settings)
    assert source_key != account_key


@pytest.mark.unit
@pytest.mark.parametrize(
    ("secret", "field"),
    [
        ("", "admin_login_limiter_secret"),
        ("short-secret", "admin_login_limiter_secret"),
        ("changeme", "admin_login_limiter_secret"),
        ("placeholder", "admin_login_limiter_secret_previous"),
    ],
)
def test_limiter_secret_validation_rejects_weak_material(
    secret: str,
    field: str,
) -> None:
    with pytest.raises(ValueError, match=field):
        admin_auth.validate_admin_login_limiter_secret(secret, field=field)


@pytest.mark.unit
def test_limiter_configuration_rejects_matching_previous_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_secrets(
        monkeypatch,
        limiter_secret=TEST_LIMITER_SECRET,
        previous_secret=TEST_LIMITER_SECRET,
    )
    with pytest.raises(ValueError, match="must differ"):
        admin_auth.validate_admin_login_limiter_configuration(settings)


@pytest.mark.integration
def test_rotation_previous_secret_lockout_still_denies_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _require_database_url()
    now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)

    with psycopg.connect(database_url, autocommit=False) as bootstrap:
        bootstrap.execute("DROP SCHEMA IF EXISTS public CASCADE")
        bootstrap.execute("CREATE SCHEMA public")
        bootstrap.commit()
        apply_migrations(bootstrap)

    previous_settings = _settings_with_secrets(
        monkeypatch,
        limiter_secret=PREVIOUS_LIMITER_SECRET,
    )
    legacy_key = admin_auth.build_source_rate_limit_key(CLIENT_SOURCE, previous_settings)

    with psycopg.connect(database_url, row_factory=dict_row, autocommit=False) as conn:
        for index in range(5):
            db.try_admit_admin_login(
                conn,
                limiter_keys=(legacy_key,),
                now=now + timedelta(seconds=index),
                rate_limit=5,
                window_seconds=900,
                lockout_seconds=900,
            )

    rotated_settings = _settings_with_secrets(
        monkeypatch,
        limiter_secret=TEST_LIMITER_SECRET,
        previous_secret=PREVIOUS_LIMITER_SECRET,
    )
    current_key = admin_auth.build_source_rate_limit_key(CLIENT_SOURCE, rotated_settings)
    lock_check_keys = admin_auth.login_limiter_lock_check_keys(
        submitted_username=CANDIDATE_USERNAME,
        client_source=CLIENT_SOURCE,
        configured_admin_username=rotated_settings.admin_username,
        settings=rotated_settings,
    )
    assert legacy_key in lock_check_keys
    assert current_key in lock_check_keys

    with psycopg.connect(database_url, row_factory=dict_row, autocommit=False) as conn:
        denied = db.try_admit_admin_login(
            conn,
            limiter_keys=(current_key,),
            lock_check_keys=lock_check_keys,
            now=now + timedelta(seconds=30),
            rate_limit=5,
            window_seconds=900,
            lockout_seconds=900,
        )
        assert not denied.admitted
        assert denied.already_locked

        deleted = db.cleanup_expired_admin_login_rate_limits(
            conn,
            now=now + timedelta(days=30),
            window_seconds=900,
            lockout_seconds=900,
        )
        assert deleted >= 1


@pytest.mark.unit
@pytest.mark.integration
def test_unknown_username_failure_audit_is_anonymous_without_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_admin_auth import FakeRateLimitStore, shared_rate_limiter

    store = FakeRateLimitStore()
    captured: list[dict[str, Any]] = []

    def _capture_failure(conn: Any, **kwargs: Any) -> dict[str, Any]:
        captured.append(kwargs)
        return {"id": "evt-failure"}

    with shared_rate_limiter(store):
        with mock_db_connection():
            with patch(
                "app.admin_routes.audit_service.record_login_failure",
                side_effect=_capture_failure,
            ):
                with patch(
                    "app.admin_routes._try_claim_login_flow",
                    return_value=True,
                ):
                    response = client.post(
                        "/admin/login",
                        data={
                            "username": CANDIDATE_USERNAME,
                            "password": "wrong-password",
                            "csrf_token": "flow-csrf",
                        },
                    )
    assert response.status_code == 401
    assert captured
    actor_context = captured[-1]["actor_context"]
    assert actor_context.actor == "anonymous"
    assert CANDIDATE_USERNAME not in json.dumps(captured[-1], default=str)


@pytest.mark.unit
@pytest.mark.integration
def test_configured_username_wrong_password_audit_is_anonymous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_admin_auth import FakeRateLimitStore, shared_rate_limiter

    store = FakeRateLimitStore()
    captured: list[dict[str, Any]] = []

    def _capture_failure(conn: Any, **kwargs: Any) -> dict[str, Any]:
        captured.append(kwargs)
        return {"id": "evt-failure"}

    with shared_rate_limiter(store):
        with mock_db_connection():
            with patch(
                "app.admin_routes.audit_service.record_login_failure",
                side_effect=_capture_failure,
            ):
                with patch(
                    "app.admin_routes._try_claim_login_flow",
                    return_value=True,
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
    actor_context = captured[-1]["actor_context"]
    assert actor_context.actor == "anonymous"
    assert TEST_USERNAME not in json.dumps(captured[-1], default=str)


@pytest.mark.unit
@pytest.mark.integration
def test_invalid_csrf_failure_audit_is_anonymous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_admin_auth import FakeRateLimitStore, shared_rate_limiter

    store = FakeRateLimitStore()
    captured: list[dict[str, Any]] = []

    with shared_rate_limiter(store):
        with mock_db_connection():
            with patch(
                "app.admin_routes.audit_service.record_login_failure",
                side_effect=lambda _conn, **kwargs: captured.append(kwargs) or {"id": "evt"},
            ):
                with patch("app.admin_routes._try_claim_login_flow", return_value=False):
                    response = client.post(
                        "/admin/login",
                        data={
                            "username": CANDIDATE_USERNAME,
                            "password": TEST_PASSWORD,
                            "csrf_token": "bad-csrf",
                        },
                    )
    assert response.status_code == 400
    assert captured[-1]["reason"] == "invalid_csrf"
    assert captured[-1]["actor_context"].actor == "anonymous"


@pytest.mark.unit
@pytest.mark.integration
def test_successful_login_audit_retains_administrator_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_admin_auth import FakeRateLimitStore, shared_rate_limiter

    store = FakeRateLimitStore()
    captured: list[dict[str, Any]] = []

    with shared_rate_limiter(store):
        with mock_db_connection():
            with patch(
                "app.admin_routes.audit_service.record_login_success",
                side_effect=lambda _conn, **kwargs: captured.append(kwargs) or {"id": "evt"},
            ):
                with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                    with patch("app.admin_routes.db.create_admin_session", return_value=77):
                        response = client.post(
                            "/admin/login",
                            data={
                                "username": TEST_USERNAME,
                                "password": TEST_PASSWORD,
                                "csrf_token": "flow-csrf",
                            },
                        )
    assert response.status_code == 303
    assert captured[-1]["actor_context"].actor == TEST_USERNAME
    assert captured[-1]["session_id"] == 77


@pytest.mark.unit
def test_login_failure_logs_do_not_leak_candidates_or_secrets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from tests.test_admin_auth import FakeRateLimitStore, shared_rate_limiter

    store = FakeRateLimitStore()
    caplog.set_level(logging.DEBUG)

    with shared_rate_limiter(store):
        with mock_db_connection():
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                client.post(
                    "/admin/login",
                    data={
                        "username": CANDIDATE_USERNAME,
                        "password": "wrong-password",
                        "csrf_token": "flow-csrf",
                    },
                )

    combined = caplog.text
    for forbidden in (
        CANDIDATE_USERNAME,
        TEST_LIMITER_SECRET,
        CLIENT_SOURCE,
        f"{admin_auth.LIMITER_DOMAIN_SOURCE}:",
    ):
        assert forbidden not in combined


@pytest.mark.integration
def test_postgres_persists_keyed_limiter_identifier_and_anonymous_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _require_database_url()
    settings = _settings_with_secrets(monkeypatch)
    source_key = admin_auth.build_source_rate_limit_key(CLIENT_SOURCE, settings)
    plain = admin_auth.plain_sha256_limiter_key(
        admin_auth.LIMITER_DOMAIN_SOURCE,
        CLIENT_SOURCE.strip().lower(),
    )
    assert source_key != plain

    with psycopg.connect(database_url, autocommit=False) as bootstrap:
        bootstrap.execute("DROP SCHEMA IF EXISTS public CASCADE")
        bootstrap.execute("CREATE SCHEMA public")
        bootstrap.commit()
        apply_migrations(bootstrap)

    repo = PostgresAuditEventRepository()
    now = datetime(2026, 7, 15, 15, 0, tzinfo=timezone.utc)

    with psycopg.connect(database_url, row_factory=dict_row, autocommit=False) as conn:
        db.try_admit_admin_login(
            conn,
            limiter_keys=(source_key,),
            now=now,
            rate_limit=5,
            window_seconds=900,
            lockout_seconds=900,
        )
        with crm_transaction(conn):
            audit_service.record_login_failure(
                conn,
                actor_context=ActorContext(actor="anonymous", correlation_id="corr-242"),
                reason="invalid_credentials",
                repository=repo,
            )

        with conn.cursor() as cur:
            cur.execute(
                "SELECT limiter_key FROM admin_login_rate_limits WHERE limiter_key = %s",
                (source_key,),
            )
            limiter_row = cur.fetchone()
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

    assert limiter_row is not None
    assert limiter_row["limiter_key"] == source_key
    assert limiter_row["limiter_key"] != plain
    assert audit_row is not None
    assert audit_row["actor"] == "anonymous"
    serialized = json.dumps(
        {
            "summary_after": audit_row["summary_after"],
            "metadata": audit_row["metadata"],
        },
        default=str,
    )
    assert CANDIDATE_USERNAME not in serialized
    assert TEST_LIMITER_SECRET not in serialized
