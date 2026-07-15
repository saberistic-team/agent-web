"""Tests for HMAC login limiter identifiers and anonymous failed-login actors."""

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
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from app import admin_auth, audit_service, db
from app.admin_secrets import (
    validate_admin_login_limiter_secret,
    validate_admin_security_config,
)
from app.admin_routes import _record_login_failure
from app.config import get_settings
from app.crm_uow import crm_transaction
from app.main import app
from app.migrations.runner import apply_migrations
from tests.conftest import TEST_LIMITER_SECRET, TEST_LIMITER_SECRET_ALT
from tests.test_admin_auth import (
    TEST_HASH,
    TEST_LIMITER_SECRET as AUTH_TEST_LIMITER_SECRET,
    TEST_PASSWORD,
    TEST_SECRET,
    TEST_USERNAME,
    mock_db_connection,
    shared_rate_limiter,
)

client = TestClient(app, follow_redirects=False)

_REQUIRED = (os.environ.get("REQUIRE_TEST_DATABASE") or "").strip() in {"1", "true", "yes"}
_DATABASE_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip()


def _plain_sha256_digest(domain: str, material: str) -> str:
    return hashlib.sha256(f"{domain}:{material}".encode("utf-8")).hexdigest()


@pytest.fixture
def limiter_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", AUTH_TEST_LIMITER_SECRET)
    monkeypatch.delenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", raising=False)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    admin_auth.reset_login_rate_limiter()


@pytest.mark.unit
def test_limiter_identifier_is_not_plain_sha256(limiter_env: None) -> None:
    settings = get_settings()
    material = "203.0.113.50"
    keyed = admin_auth.build_source_rate_limit_key(material, settings)
    plain = _plain_sha256_digest("src", material)
    assert keyed != plain
    assert len(keyed) == 64


@pytest.mark.unit
def test_limiter_identifier_depends_on_secret(limiter_env: None) -> None:
    settings_a = get_settings()
    with patch.dict(
        os.environ,
        {"ADMIN_LOGIN_LIMITER_SECRET": TEST_LIMITER_SECRET_ALT},
        clear=False,
    ):
        from app.config import Settings

        settings_b = Settings(
            database_url=settings_a.database_url,
            stripe_secret_key="",
            stripe_webhook_secret="",
            stripe_publishable_key="",
            resend_api_key="",
            from_email="",
            notify_email="",
            base_url=settings_a.base_url,
            plausible_domain="",
            plausible_api_key="",
            analytics_environment="development",
            admin_username=settings_a.admin_username,
            admin_password_hash=settings_a.admin_password_hash,
            admin_session_secret=settings_a.admin_session_secret,
            admin_login_limiter_secret=TEST_LIMITER_SECRET_ALT,
            admin_login_limiter_secret_previous="",
        )
        key_a = admin_auth.build_source_rate_limit_key("203.0.113.50", settings_a)
        key_b = admin_auth.build_source_rate_limit_key("203.0.113.50", settings_b)
    assert key_a != key_b


@pytest.mark.unit
def test_limiter_identifier_stable_across_calls(limiter_env: None) -> None:
    settings = get_settings()
    first = admin_auth.build_source_rate_limit_key("203.0.113.50", settings)
    second = admin_auth.build_source_rate_limit_key("203.0.113.50", settings)
    assert first == second


@pytest.mark.unit
def test_limiter_domain_separation(limiter_env: None) -> None:
    settings = get_settings()
    shared_material = "203.0.113.50"
    source_key = admin_auth.build_source_rate_limit_key(shared_material, settings)
    account_key = admin_auth.build_account_rate_limit_key(shared_material, settings)
    assert source_key != account_key


@pytest.mark.unit
@pytest.mark.parametrize(
    ("secret", "label_fragment"),
    [
        ("", "required"),
        ("short", "at least"),
        ("changeme-changeme-changeme-changeme", "placeholder"),
        ("placeholder-placeholder-placeholder-pl", "placeholder"),
        ("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "entropy"),
    ],
)
def test_limiter_secret_validation_rejects_weak_material(
    secret: str,
    label_fragment: str,
) -> None:
    with pytest.raises(ValueError, match=label_fragment):
        validate_admin_login_limiter_secret(secret)


@pytest.mark.unit
def test_startup_validation_requires_limiter_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.delenv("ADMIN_LOGIN_LIMITER_SECRET", raising=False)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    settings = get_settings()
    with pytest.raises(ValueError, match="ADMIN_LOGIN_LIMITER_SECRET"):
        validate_admin_security_config(settings)


@pytest.mark.unit
def test_rotation_includes_previous_secret_keys(
    monkeypatch: pytest.MonkeyPatch,
    limiter_env: None,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", TEST_LIMITER_SECRET_ALT)
    settings = get_settings()
    keys = admin_auth.login_limiter_keys(
        settings=settings,
        submitted_username=TEST_USERNAME,
        client_source="203.0.113.1",
    )
    assert len(keys) == 4
    current_source = admin_auth.build_source_rate_limit_key("203.0.113.1", settings)
    previous_source = admin_auth.digest_limiter_key(
        TEST_LIMITER_SECRET_ALT,
        admin_auth.LIMITER_DOMAIN_SOURCE,
        "203.0.113.1",
    )
    assert current_source in keys
    assert previous_source in keys


@pytest.mark.unit
def test_rotation_rejects_identical_current_and_previous(
    monkeypatch: pytest.MonkeyPatch,
    limiter_env: None,
) -> None:
    monkeypatch.setenv(
        "ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS",
        AUTH_TEST_LIMITER_SECRET,
    )
    settings = get_settings()
    with pytest.raises(ValueError, match="must differ"):
        validate_admin_security_config(settings)


class _AuditSpyRepository:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def append(self, **kwargs: Any) -> dict[str, Any]:
        self.events.append(kwargs)
        return {"id": "evt-spy"}


@pytest.mark.unit
def test_record_login_failure_uses_anonymous_actor(limiter_env: None) -> None:
    conn = MagicMock()
    repo = _AuditSpyRepository()
    request = MagicMock()
    request.headers = {"x-request-id": "trace-login-fail"}
    request.state = MagicMock()
    request.state.correlation_id = "trace-login-fail"

    with patch("app.admin_routes.get_settings", return_value=get_settings()):
        with patch("app.admin_routes.db.db_connection") as db_conn:
            db_conn.return_value.__enter__.return_value = conn
            with patch("app.admin_routes.crm_transaction", wraps=crm_transaction):
                with patch(
                    "app.admin_routes.audit_service.record_login_failure",
                    wraps=audit_service.record_login_failure,
                ) as failure_audit:
                    _record_login_failure(request, reason="invalid_credentials")

    failure_audit.assert_called_once()
    actor_context = failure_audit.call_args.kwargs["actor_context"]
    assert actor_context.actor == "anonymous"
    assert failure_audit.call_args.kwargs["reason"] == "invalid_credentials"


@pytest.mark.unit
@pytest.mark.integration
def test_unknown_username_failure_audit_is_anonymous(limiter_env: None) -> None:
    from tests.test_admin_auth import _fetch_login_form

    repo = _AuditSpyRepository()
    attacker = "attacker-candidate@evil.example"
    with mock_db_connection():
        with patch("app.admin_routes._try_claim_login_flow", return_value=True):
            with patch(
                "app.admin_routes.audit_service.record_login_failure",
                side_effect=lambda conn, **kwargs: repo.append(**kwargs),
            ):
                csrf_token, cookies = _fetch_login_form()
                response = client.post(
                    "/admin/login",
                    data={
                        "username": attacker,
                        "password": "wrong-password",
                        "csrf_token": csrf_token,
                    },
                    cookies=cookies,
                )
    assert response.status_code == 401
    assert repo.events
    event = repo.events[-1]
    assert event["actor_context"].actor == "anonymous"
    payload = json.dumps(event, default=str)
    assert attacker not in payload


@pytest.mark.unit
@pytest.mark.integration
def test_configured_username_wrong_password_audit_is_anonymous(
    limiter_env: None,
) -> None:
    from tests.test_admin_auth import _fetch_login_form

    repo = _AuditSpyRepository()
    with mock_db_connection():
        with patch("app.admin_routes._try_claim_login_flow", return_value=True):
            with patch(
                "app.admin_routes.audit_service.record_login_failure",
                side_effect=lambda conn, **kwargs: repo.append(**kwargs),
            ):
                csrf_token, cookies = _fetch_login_form()
                response = client.post(
                    "/admin/login",
                    data={
                        "username": TEST_USERNAME,
                        "password": "wrong-password",
                        "csrf_token": csrf_token,
                    },
                    cookies=cookies,
                )
    assert response.status_code == 401
    assert repo.events
    assert repo.events[-1]["actor_context"].actor == "anonymous"
    assert TEST_USERNAME not in json.dumps(repo.events[-1], default=str)


@pytest.mark.unit
@pytest.mark.integration
def test_invalid_csrf_failure_audit_is_anonymous(limiter_env: None) -> None:
    from tests.test_admin_auth import _fetch_login_form

    repo = _AuditSpyRepository()
    with mock_db_connection():
        with patch(
            "app.admin_routes.audit_service.record_login_failure",
            side_effect=lambda conn, **kwargs: repo.append(**kwargs),
        ):
            csrf_token, cookies = _fetch_login_form()
            response = client.post(
                "/admin/login",
                data={
                    "username": "csrf-attacker",
                    "password": TEST_PASSWORD,
                    "csrf_token": "tampered-token",
                },
                cookies=cookies,
            )
    assert response.status_code == 400
    assert repo.events
    assert repo.events[-1]["actor_context"].actor == "anonymous"
    assert "csrf-attacker" not in json.dumps(repo.events[-1], default=str)


@pytest.mark.unit
@pytest.mark.integration
def test_successful_login_retains_administrator_actor(limiter_env: None) -> None:
    from tests.test_admin_auth import _fetch_login_form

    with mock_db_connection():
        with patch("app.admin_routes._try_claim_login_flow", return_value=True):
            with patch("app.admin_routes.db.create_admin_session", return_value=42):
                with patch(
                    "app.admin_routes.audit_service.record_login_success"
                ) as success_audit:
                    csrf_token, cookies = _fetch_login_form()
                    response = client.post(
                        "/admin/login",
                        data={
                            "username": TEST_USERNAME,
                            "password": TEST_PASSWORD,
                            "csrf_token": csrf_token,
                        },
                        cookies=cookies,
                    )
    assert response.status_code == 303
    success_audit.assert_called_once()
    assert success_audit.call_args.kwargs["actor_context"].actor == TEST_USERNAME
    assert success_audit.call_args.kwargs["session_id"] == 42


@pytest.mark.unit
@pytest.mark.integration
def test_failed_login_logs_exclude_candidate_and_secret(
    limiter_env: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from tests.test_admin_auth import FakeRateLimitStore, _fetch_login_form

    caplog.set_level(logging.INFO)
    store = FakeRateLimitStore()
    attacker = "logged-candidate-user"
    secret = AUTH_TEST_LIMITER_SECRET

    with shared_rate_limiter(store):
        with mock_db_connection():
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                csrf_token, cookies = _fetch_login_form()
                client.post(
                    "/admin/login",
                    data={
                        "username": attacker,
                        "password": "wrong-password",
                        "csrf_token": csrf_token,
                    },
                    cookies=cookies,
                )

    combined = caplog.text
    assert attacker not in combined
    assert secret not in combined


def _require_database_url() -> str:
    if _DATABASE_URL:
        return _DATABASE_URL
    if _REQUIRED:
        pytest.fail("REQUIRE_TEST_DATABASE=1 but TEST_DATABASE_URL is unset")
    pytest.skip("TEST_DATABASE_URL not set; skipping live Postgres limiter tests")


@contextmanager
def _connect(database_url: str) -> Generator[psycopg.Connection, None, None]:
    conn = psycopg.connect(database_url, row_factory=dict_row, autocommit=False)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def pg_conn() -> Generator[psycopg.Connection, None, None]:
    database_url = _require_database_url()
    with psycopg.connect(database_url, autocommit=False) as bootstrap:
        bootstrap.execute("DROP SCHEMA IF EXISTS public CASCADE")
        bootstrap.execute("CREATE SCHEMA public")
        bootstrap.execute("GRANT ALL ON SCHEMA public TO CURRENT_USER")
        bootstrap.execute("GRANT ALL ON SCHEMA public TO public")
        apply_migrations(bootstrap)
    with _connect(database_url) as conn:
        try:
            yield conn
        finally:
            conn.rollback()
            with psycopg.connect(database_url, autocommit=False) as cleanup:
                cleanup.execute("DROP SCHEMA IF EXISTS public CASCADE")
                cleanup.execute("CREATE SCHEMA public")
                cleanup.commit()


@pytest.mark.integration
def test_postgres_persists_hmac_limiter_key_and_anonymous_actor(
    pg_conn: psycopg.Connection,
    limiter_env: None,
) -> None:
    settings = get_settings()
    source = "203.0.113.88"
    source_key = admin_auth.build_source_rate_limit_key(source, settings)
    plain = _plain_sha256_digest("src", source)
    assert source_key != plain

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
            """
            SELECT limiter_key
            FROM admin_login_rate_limits
            WHERE limiter_key = %s
            """,
            (source_key,),
        )
        row = cur.fetchone()
    assert row is not None
    assert row["limiter_key"] == source_key
    assert len(row["limiter_key"]) == 64

    repo = _AuditSpyRepository()

    request = MagicMock()
    request.headers = {}
    request.state = MagicMock()
    request.state.correlation_id = "pg-audit-corr"

    with patch("app.admin_routes.get_settings", return_value=settings):
        with patch("app.admin_routes.db.db_connection") as db_conn:
            db_conn.return_value.__enter__.return_value = pg_conn
            with patch("app.admin_routes.crm_transaction", wraps=crm_transaction):
                with patch(
                    "app.admin_routes.audit_service.record_login_failure",
                    side_effect=lambda conn, **kwargs: repo.append(**kwargs)
                    or audit_service.record_login_failure(conn, **kwargs),
                ):
                    _record_login_failure(request, reason="invalid_credentials")

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
    assert "invalid_credentials" in str(audit_row["summary_after"])
    assert "attacker" not in str(audit_row).lower()


@pytest.mark.integration
def test_rotation_previous_key_rows_remain_eligible_for_cleanup(
    pg_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    limiter_env: None,
) -> None:
    settings = get_settings()
    old_secret = TEST_LIMITER_SECRET_ALT
    source = "203.0.113.99"
    old_key = admin_auth.digest_limiter_key(
        old_secret,
        admin_auth.LIMITER_DOMAIN_SOURCE,
        source,
    )
    now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    db.try_admit_admin_login(
        pg_conn,
        limiter_keys=(old_key,),
        now=now,
        rate_limit=5,
        window_seconds=60,
        lockout_seconds=60,
    )

    deleted = db.cleanup_expired_admin_login_rate_limits(
        pg_conn,
        now=now + timedelta(seconds=200),
        window_seconds=60,
        lockout_seconds=60,
    )
    assert deleted >= 1

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS count FROM admin_login_rate_limits WHERE limiter_key = %s",
            (old_key,),
        )
        row = cur.fetchone()
    assert row is not None
    assert int(row["count"]) == 0

    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", old_secret)
    rotated = get_settings()
    keys = admin_auth.login_limiter_keys(
        settings=rotated,
        submitted_username=TEST_USERNAME,
        client_source=source,
    )
    assert old_key in keys
