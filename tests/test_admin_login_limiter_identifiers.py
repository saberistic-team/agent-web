"""Tests for keyed admin login limiter identifiers and anonymous failure actors."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator
from unittest.mock import MagicMock, patch

import psycopg
import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from app import admin_auth, audit_service, db
from app.actor_context import ActorContext
from app.admin_security import (
    LIMITER_DOMAIN_SOURCE,
    digest_limiter_key,
    validate_admin_security_config,
    validate_limiter_secret,
)
from app.config import Settings, get_settings
from app.crm_uow import crm_transaction
from app.main import app
from app.migrations.runner import apply_migrations
from tests.conftest import TEST_LIMITER_SECRET

client = TestClient(app, follow_redirects=False)

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SESSION_SECRET = "test-session-secret-32chars-minimum"
ALT_LIMITER_SECRET = "alt-limiter-secret-32chars-minimum!!"
PREVIOUS_LIMITER_SECRET = "previous-limiter-secret-32chars-min!!"

_REQUIRED = (os.environ.get("REQUIRE_TEST_DATABASE") or "").strip() in {"1", "true", "yes"}
_DATABASE_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip()


def _settings(**overrides: Any) -> Settings:
    base = get_settings()
    values = {
        "database_url": base.database_url,
        "stripe_secret_key": base.stripe_secret_key,
        "stripe_webhook_secret": base.stripe_webhook_secret,
        "stripe_publishable_key": base.stripe_publishable_key,
        "resend_api_key": base.resend_api_key,
        "from_email": base.from_email,
        "notify_email": base.notify_email,
        "base_url": base.base_url,
        "plausible_domain": base.plausible_domain,
        "plausible_api_key": base.plausible_api_key,
        "analytics_environment": base.analytics_environment,
        "admin_username": TEST_USERNAME,
        "admin_password_hash": TEST_HASH,
        "admin_session_secret": TEST_SESSION_SECRET,
        "admin_login_limiter_secret": TEST_LIMITER_SECRET,
        "admin_login_limiter_previous_secret": "",
    }
    values.update(overrides)
    return Settings(**values)


def _plain_sha256_identifier(prefix: str, material: str) -> str:
    payload = f"{prefix}:{material}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@pytest.fixture(autouse=True)
def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SESSION_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.delenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", raising=False)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    admin_auth.reset_login_rate_limiter()


def _require_database_url() -> str:
    if _DATABASE_URL:
        return _DATABASE_URL
    if _REQUIRED:
        pytest.fail("REQUIRE_TEST_DATABASE=1 but TEST_DATABASE_URL is unset")
    pytest.skip("TEST_DATABASE_URL not set; skipping live Postgres login limiter tests")


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
def pg_conn() -> Iterator[psycopg.Connection]:
    database_url = _require_database_url()
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
def test_persisted_identifier_is_not_plain_sha256() -> None:
    settings = _settings()
    source = "203.0.113.10"
    key = admin_auth.build_source_rate_limit_key(source, settings=settings)
    plain = _plain_sha256_identifier(admin_auth.LIMITER_DOMAIN_SOURCE, source)
    assert key != plain
    assert len(key) == 64


@pytest.mark.unit
def test_identifier_depends_on_secret() -> None:
    settings_a = _settings(admin_login_limiter_secret=TEST_LIMITER_SECRET)
    settings_b = _settings(admin_login_limiter_secret=ALT_LIMITER_SECRET)
    source = "203.0.113.10"
    key_a = admin_auth.build_source_rate_limit_key(source, settings=settings_a)
    key_b = admin_auth.build_source_rate_limit_key(source, settings=settings_b)
    assert key_a != key_b


@pytest.mark.unit
def test_identifier_is_stable_for_same_input_secret_and_domain() -> None:
    settings = _settings()
    source = "203.0.113.10"
    first = admin_auth.build_source_rate_limit_key(source, settings=settings)
    second = admin_auth.build_source_rate_limit_key(source, settings=settings)
    assert first == second


@pytest.mark.unit
def test_domain_separation_for_identical_payload_material() -> None:
    settings = _settings()
    material = "operator"
    source_key = admin_auth.build_source_rate_limit_key(material, settings=settings)
    candidate_key = admin_auth.build_candidate_rate_limit_key(material, settings=settings)
    assert source_key != candidate_key


@pytest.mark.unit
@pytest.mark.parametrize(
    ("secret", "env_name", "match"),
    [
        ("", "ADMIN_LOGIN_LIMITER_SECRET", "required"),
        ("short-secret", "ADMIN_LOGIN_LIMITER_SECRET", "at least"),
        ("admin-login-limiter-secret", "ADMIN_LOGIN_LIMITER_SECRET", "placeholder"),
    ],
)
def test_limiter_secret_validation_rejects_weak_material(
    secret: str,
    env_name: str,
    match: str,
) -> None:
    with pytest.raises(ValueError, match=match):
        validate_limiter_secret(secret, env_name=env_name)


@pytest.mark.unit
def test_limiter_configuration_rejects_matching_previous_secret() -> None:
    settings = _settings(
        admin_login_limiter_secret=TEST_LIMITER_SECRET,
        admin_login_limiter_previous_secret=TEST_LIMITER_SECRET,
    )
    with pytest.raises(ValueError, match="must differ"):
        validate_admin_security_config(settings)


@pytest.mark.unit
def test_startup_validation_fails_for_missing_limiter_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.delenv("ADMIN_LOGIN_LIMITER_SECRET", raising=False)
    settings = get_settings()
    with pytest.raises(ValueError, match="ADMIN_LOGIN_LIMITER_SECRET is required"):
        validate_admin_security_config(settings)


@pytest.mark.unit
def test_rotation_guard_keys_include_previous_secret_only_for_reads() -> None:
    settings = _settings(
        admin_login_limiter_secret=ALT_LIMITER_SECRET,
        admin_login_limiter_previous_secret=PREVIOUS_LIMITER_SECRET,
    )
    guard = admin_auth.login_limiter_rotation_keys(
        settings=settings,
        submitted_username=TEST_USERNAME,
        client_source="203.0.113.10",
        configured_admin_username=TEST_USERNAME,
    )
    write = admin_auth.login_limiter_keys(
        settings=settings,
        submitted_username=TEST_USERNAME,
        client_source="203.0.113.10",
        configured_admin_username=TEST_USERNAME,
    )
    previous_source = digest_limiter_key(
        domain=LIMITER_DOMAIN_SOURCE,
        material="203.0.113.10",
        secret=PREVIOUS_LIMITER_SECRET,
    )
    assert previous_source in guard
    assert previous_source not in write
    assert len(write) == 2


@pytest.mark.integration
def test_rotation_previous_key_lockout_blocks_without_incrementing_new_key(
    pg_conn: psycopg.Connection,
) -> None:
    settings = _settings(
        admin_login_limiter_secret=ALT_LIMITER_SECRET,
        admin_login_limiter_previous_secret=PREVIOUS_LIMITER_SECRET,
    )
    now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    previous_source = digest_limiter_key(
        domain=LIMITER_DOMAIN_SOURCE,
        material="203.0.113.10",
        secret=PREVIOUS_LIMITER_SECRET,
    )
    current_source = admin_auth.build_source_rate_limit_key("203.0.113.10", settings=settings)
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
                now - timedelta(minutes=1),
                now + timedelta(minutes=15),
                now,
            ),
        )
    pg_conn.commit()

    guard = admin_auth.login_limiter_rotation_keys(
        settings=settings,
        submitted_username="ghost",
        client_source="203.0.113.10",
        configured_admin_username=TEST_USERNAME,
    )
    write = admin_auth.login_limiter_keys(
        settings=settings,
        submitted_username="ghost",
        client_source="203.0.113.10",
        configured_admin_username=TEST_USERNAME,
    )
    blocked = db.try_admit_admin_login(
        pg_conn,
        limiter_keys=write,
        guard_keys=guard,
        now=now,
        rate_limit=5,
        window_seconds=900,
        lockout_seconds=900,
    )
    assert not blocked.admitted
    assert blocked.already_locked

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT failure_count FROM admin_login_rate_limits WHERE limiter_key = %s",
            (current_source,),
        )
        row = cur.fetchone()
    assert row is None or int(row["failure_count"]) == 0


@pytest.mark.integration
def test_expired_previous_key_rows_are_eligible_for_cleanup(pg_conn: psycopg.Connection) -> None:
    now = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    stale_key = "deadbeef" * 8
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO admin_login_rate_limits (
                limiter_key, failure_count, window_started_at, locked_until, updated_at
            )
            VALUES (%s, 1, %s, NULL, %s)
            """,
            (stale_key, now - timedelta(hours=2), now - timedelta(hours=2)),
        )
    pg_conn.commit()
    deleted = db.cleanup_expired_admin_login_rate_limits(
        pg_conn,
        now=now,
        window_seconds=900,
        lockout_seconds=900,
    )
    assert deleted == 1


@pytest.mark.integration
def test_concurrent_admission_with_hmac_keys_respects_threshold(pg_conn: psycopg.Connection) -> None:
    settings = _settings()
    source_key = admin_auth.build_source_rate_limit_key("198.51.100.20", settings=settings)
    now = datetime(2026, 2, 1, 9, 0, tzinfo=timezone.utc)
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
def test_unknown_username_failure_audit_uses_anonymous_actor_only() -> None:
    with patch("app.admin_routes._try_claim_login_flow", return_value=True):
        with patch("app.admin_routes.audit_service.record_login_failure") as failure_audit:
            with patch("app.admin_routes.db.db_connection") as db_conn:
                db_conn.return_value.__enter__.return_value = MagicMock()
                with patch("app.admin_routes.crm_transaction", wraps=crm_transaction):
                    with patch(
                        "app.admin_auth.try_admit_login_attempt",
                        return_value=admin_auth.LoginAdmissionResult(
                            admitted=True,
                            throttled=False,
                            already_locked=False,
                            lockout_transition=False,
                        ),
                    ):
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
    assert "ghost-attacker" not in str(failure_audit.call_args)


@pytest.mark.unit
@pytest.mark.integration
def test_configured_username_wrong_password_keeps_anonymous_actor() -> None:
    captured: dict[str, Any] = {}

    def _capture(_conn: Any, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"id": "evt-2"}

    with patch("app.admin_routes._try_claim_login_flow", return_value=True):
        with patch(
            "app.admin_routes.audit_service.record_login_failure",
            side_effect=_capture,
        ):
            with patch("app.admin_routes.db.db_connection") as db_conn:
                db_conn.return_value.__enter__.return_value = MagicMock()
                with patch("app.admin_routes.crm_transaction", wraps=crm_transaction):
                    with patch(
                        "app.admin_auth.try_admit_login_attempt",
                        return_value=admin_auth.LoginAdmissionResult(
                            admitted=True,
                            throttled=False,
                            already_locked=False,
                            lockout_transition=False,
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
    actor_context = captured["actor_context"]
    assert actor_context.actor == "anonymous"
    assert TEST_USERNAME not in str(captured)


@pytest.mark.unit
@pytest.mark.integration
def test_invalid_flow_and_lockout_audit_events_remain_anonymous() -> None:
    calls: list[ActorContext] = []

    def _capture(_conn: Any, *, actor_context: ActorContext, reason: str, **_: Any) -> None:
        calls.append(actor_context)

    with patch(
        "app.admin_routes.audit_service.record_login_failure",
        side_effect=_capture,
    ):
        with patch("app.admin_routes._try_claim_login_flow", return_value=False):
            with patch("app.admin_routes._try_burn_login_flow_cookie", return_value=None):
                with patch("app.admin_routes.db.db_connection") as db_conn:
                    db_conn.return_value.__enter__.return_value = MagicMock()
                    with patch(
                        "app.admin_auth.try_admit_login_attempt",
                        return_value=admin_auth.LoginAdmissionResult(
                            admitted=True,
                            throttled=False,
                            already_locked=False,
                            lockout_transition=True,
                        ),
                    ):
                        csrf_response = client.post(
                            "/admin/login",
                            data={
                                "username": "attacker-name",
                                "password": "wrong-password",
                                "csrf_token": "flow-csrf",
                            },
                        )
    assert csrf_response.status_code == 400
    assert calls
    assert all(ctx.actor == "anonymous" for ctx in calls)


@pytest.mark.unit
@pytest.mark.integration
def test_successful_login_retains_administrator_actor() -> None:
    with patch("app.admin_routes._try_claim_login_flow", return_value=True):
        with patch("app.admin_routes.db.create_admin_session", return_value=42):
            with patch("app.admin_routes.db.db_connection") as db_conn:
                db_conn.return_value.__enter__.return_value = MagicMock()
                with patch("app.admin_routes.crm_transaction", wraps=crm_transaction):
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
    assert success_audit.call_args.kwargs["actor_context"].actor == TEST_USERNAME
    assert success_audit.call_args.kwargs["session_id"] == 42


@pytest.mark.unit
@pytest.mark.integration
def test_failed_login_logs_do_not_include_candidates_or_secrets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    with patch("app.admin_routes._try_claim_login_flow", return_value=True):
        with patch("app.admin_routes.db.db_connection") as db_conn:
            db_conn.return_value.__enter__.return_value = MagicMock()
            with patch("app.admin_routes.crm_transaction", wraps=crm_transaction):
                with patch(
                    "app.admin_auth.try_admit_login_attempt",
                    return_value=admin_auth.LoginAdmissionResult(
                        admitted=True,
                        throttled=False,
                        already_locked=False,
                        lockout_transition=False,
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
    combined = caplog.text
    assert candidate not in combined
    assert TEST_LIMITER_SECRET not in combined
    assert "203.0.113." not in combined


@pytest.mark.integration
def test_postgres_persists_hmac_identifiers_and_anonymous_failure_actor(
    pg_conn: psycopg.Connection,
) -> None:
    settings = _settings()
    source = "203.0.113.88"
    source_key = admin_auth.build_source_rate_limit_key(source, settings=settings)
    plain = _plain_sha256_identifier(admin_auth.LIMITER_DOMAIN_SOURCE, source.lower())
    now = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    db.try_admit_admin_login(
        pg_conn,
        limiter_keys=(source_key,),
        now=now,
        rate_limit=5,
        window_seconds=900,
        lockout_seconds=900,
    )

    repo = MagicMock()
    repo.append.side_effect = lambda _conn, **kwargs: _persist_audit(pg_conn, kwargs)

    actor = ActorContext(actor="anonymous", correlation_id="corr-pg")
    audit_service.record_login_failure(
        pg_conn,
        actor_context=actor,
        reason="invalid_credentials",
        repository=repo,
    )
    pg_conn.commit()

    with pg_conn.cursor() as cur:
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
    assert len(limiter_row["limiter_key"]) == 64
    assert audit_row is not None
    assert audit_row["actor"] == "anonymous"
    audit_blob = json.dumps(
        {
            "summary_after": audit_row["summary_after"],
            "metadata": audit_row["metadata"],
        }
    )
    assert TEST_USERNAME not in audit_blob


def _persist_audit(conn: psycopg.Connection, kwargs: dict[str, Any]) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO audit_events (
                actor, action, entity_type, correlation_id, summary_after, metadata
            )
            VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb)
            RETURNING id
            """,
            (
                kwargs["actor"],
                kwargs["action"],
                kwargs.get("entity_type"),
                kwargs.get("correlation_id"),
                json.dumps(kwargs.get("summary_after") or {}),
                json.dumps(kwargs.get("metadata") or {}),
            ),
        )
        row = cur.fetchone()
    return {"id": str(row["id"]) if row else "evt"}
