"""Security tests for keyed admin login limiter identifiers and anonymous audit actors."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row
from starlette.requests import Request

from app import admin_auth, audit_service, db
from app.actor_context import anonymous_actor_context
from app.config import get_settings
from app.crm_uow import crm_transaction
from app.main import app
from app.migrations.runner import apply_migrations
from tests.conftest import TEST_LIMITER_SECRET
from tests.test_admin_auth import (
    TEST_PASSWORD,
    TEST_SECRET,
    TEST_USERNAME,
    client as auth_client,
    mock_db_connection,
    rate_limit_store,
    shared_rate_limiter,
    _fetch_login_form,
    _login,
)

client = TestClient(app, follow_redirects=False)

_REQUIRED = (os.environ.get("REQUIRE_TEST_DATABASE") or "").strip() in {"1", "true", "yes"}
_DATABASE_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip()


def _require_database_url() -> str:
    if _DATABASE_URL:
        return _DATABASE_URL
    if _REQUIRED:
        pytest.fail("REQUIRE_TEST_DATABASE=1 but TEST_DATABASE_URL is unset")
    pytest.skip("TEST_DATABASE_URL not set; skipping live Postgres limiter security tests")


@pytest.fixture
def limiter_settings(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", "unused-for-these-tests")
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.delenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", raising=False)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    return get_settings()


@pytest.mark.unit
def test_limiter_identifier_is_not_plain_sha256(limiter_settings: Any) -> None:
    source = "203.0.113.42"
    keyed = admin_auth.build_source_rate_limit_key(source, limiter_settings)
    plain = admin_auth.plain_sha256_limiter_key("src", source.strip().lower())
    assert keyed != plain
    assert len(keyed) == 64


@pytest.mark.unit
def test_limiter_identifier_depends_on_secret(
    limiter_settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "ADMIN_LOGIN_LIMITER_SECRET",
        "other-login-limiter-secret-32bytes!!",
    )
    other_settings = get_settings()
    first = admin_auth.build_source_rate_limit_key("203.0.113.1", limiter_settings)
    second = admin_auth.build_source_rate_limit_key("203.0.113.1", other_settings)
    assert first != second


@pytest.mark.unit
def test_limiter_identifier_is_stable_for_same_inputs(limiter_settings: Any) -> None:
    first = admin_auth.build_source_rate_limit_key("203.0.113.1", limiter_settings)
    second = admin_auth.build_source_rate_limit_key("203.0.113.1", limiter_settings)
    assert admin_auth.compare_limiter_keys(first, second)


@pytest.mark.unit
def test_limiter_domain_separation(limiter_settings: Any) -> None:
    material = "203.0.113.1"
    source_key = admin_auth.build_source_rate_limit_key(material, limiter_settings)
    account_key = admin_auth.build_account_rate_limit_key(material, limiter_settings)
    assert source_key != account_key


@pytest.mark.unit
@pytest.mark.parametrize(
    ("secret", "field_name"),
    [
        ("", "ADMIN_LOGIN_LIMITER_SECRET"),
        ("short-secret", "ADMIN_LOGIN_LIMITER_SECRET"),
        ("this-is-a-placeholder-secret-value-32b", "ADMIN_LOGIN_LIMITER_SECRET"),
        ("replace-me-with-random-secret-value!", "ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS"),
    ],
)
def test_limiter_secret_validation_rejects_weak_material(
    secret: str,
    field_name: str,
) -> None:
    with pytest.raises(ValueError, match=field_name):
        admin_auth.validate_admin_login_limiter_secret(secret, field_name=field_name)


@pytest.mark.unit
def test_validate_admin_security_secrets_requires_limiter_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", "hash")
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.delenv("ADMIN_LOGIN_LIMITER_SECRET", raising=False)
    settings = get_settings()
    with pytest.raises(ValueError, match="Incomplete admin authentication configuration"):
        admin_auth.validate_admin_security_secrets(settings)


@pytest.mark.unit
def test_rotation_expands_limiter_keys_for_previous_secret(
    limiter_settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = "previous-login-limiter-secret-32bytes!"
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", previous)
    settings = get_settings()
    keys = admin_auth.login_limiter_keys(
        submitted_username=TEST_USERNAME,
        client_source="203.0.113.7",
        configured_admin_username=TEST_USERNAME,
        settings=settings,
    )
    current_source = admin_auth.build_source_rate_limit_key("203.0.113.7", settings)
    previous_source = admin_auth._digest_limiter_key(
        "src",
        "203.0.113.7",
        previous,
    )
    current_account = admin_auth.build_account_rate_limit_key(TEST_USERNAME, settings)
    previous_account = admin_auth._digest_limiter_key(
        "acct",
        TEST_USERNAME,
        previous,
    )
    assert current_source in keys
    assert previous_source in keys
    assert current_account in keys
    assert previous_account in keys


@pytest.mark.unit
def test_rotation_previous_key_rows_remain_eligible_for_cleanup() -> None:
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cur.rowcount = 2
    now = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)

    deleted = db.cleanup_expired_admin_login_rate_limits(
        conn,
        now=now,
        window_seconds=60,
        lockout_seconds=60,
    )

    assert deleted == 2
    sql = cur.execute.call_args.args[0]
    assert "DELETE FROM admin_login_rate_limits" in sql
    assert "updated_at" in sql


@pytest.mark.unit
@pytest.mark.integration
def test_failed_login_audit_uses_anonymous_actor_for_unknown_username(
    rate_limit_store: Any,
) -> None:
    captured: list[dict[str, Any]] = []

    def _capture(conn: Any, **kwargs: Any) -> None:
        captured.append(kwargs)

    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            with patch("app.admin_routes.audit_service.record_login_failure", side_effect=_capture):
                response = _login(username="ghost-attacker", password="wrong-password")
    assert response.status_code == 401
    assert captured
    actor_context = captured[-1]["actor_context"]
    assert actor_context.actor == "anonymous"
    event_text = repr(captured[-1])
    assert "ghost-attacker" not in event_text


@pytest.mark.unit
@pytest.mark.integration
def test_failed_login_audit_uses_anonymous_actor_for_configured_username(
    rate_limit_store: Any,
) -> None:
    captured: list[dict[str, Any]] = []

    def _capture(conn: Any, **kwargs: Any) -> None:
        captured.append(kwargs)

    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            with patch("app.admin_routes.audit_service.record_login_failure", side_effect=_capture):
                response = _login(username=TEST_USERNAME, password="wrong-password")
    assert response.status_code == 401
    actor_context = captured[-1]["actor_context"]
    assert actor_context.actor == "anonymous"
    assert TEST_USERNAME not in repr(captured[-1])


@pytest.mark.unit
@pytest.mark.integration
def test_invalid_csrf_failure_audit_is_anonymous(rate_limit_store: Any) -> None:
    csrf_token, cookies = _fetch_login_form()
    captured: list[dict[str, Any]] = []

    def _capture(conn: Any, **kwargs: Any) -> None:
        captured.append(kwargs)

    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            with patch("app.admin_routes.audit_service.record_login_failure", side_effect=_capture):
                response = client.post(
                    "/admin/login",
                    data={
                        "username": "csrf-attacker",
                        "password": "ignored",
                        "csrf_token": "not-the-token",
                    },
                    cookies=cookies,
                )
    assert response.status_code == 400
    assert captured[-1]["actor_context"].actor == "anonymous"
    assert "csrf-attacker" not in repr(captured[-1])


@pytest.mark.unit
@pytest.mark.integration
def test_successful_login_audit_retains_administrator_actor(rate_limit_store: Any) -> None:
    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                with patch("app.admin_routes.db.create_admin_session", return_value=42):
                    with patch(
                        "app.admin_routes.audit_service.record_login_success"
                    ) as success_audit:
                        response = _login(username=TEST_USERNAME, password=TEST_PASSWORD)
    assert response.status_code == 303
    actor_context = success_audit.call_args.kwargs["actor_context"]
    assert actor_context.actor == TEST_USERNAME


@pytest.mark.unit
@pytest.mark.integration
def test_failed_login_logs_do_not_leak_candidates_or_secrets(
    rate_limit_store: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    auth_client.cookies.clear()
    caplog.set_level(logging.INFO)
    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            response = _login(username="log-leak-candidate", password="wrong-password")
    assert response.status_code == 401
    combined = caplog.text
    assert "log-leak-candidate" not in combined
    assert TEST_LIMITER_SECRET not in combined
    assert "203.0.113" not in combined


@pytest.mark.integration
def test_postgres_persists_keyed_limiter_identifiers_and_anonymous_actor() -> None:
    database_url = _require_database_url()
    candidate = "persisted-attacker-name"

    with psycopg.connect(database_url, autocommit=False) as bootstrap:
        bootstrap.execute("DROP SCHEMA IF EXISTS public CASCADE")
        bootstrap.execute("CREATE SCHEMA public")
        bootstrap.execute("GRANT ALL ON SCHEMA public TO CURRENT_USER")
        bootstrap.execute("GRANT ALL ON SCHEMA public TO public")
        apply_migrations(bootstrap)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", "unused")
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    settings = get_settings()

    source = "203.0.113.200"
    limiter_key = admin_auth.build_source_rate_limit_key(source, settings)
    plain = admin_auth.plain_sha256_limiter_key("src", source)
    assert limiter_key != plain

    now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    audit_repo = audit_service.PostgresAuditEventRepository()

    try:
        with psycopg.connect(database_url, row_factory=dict_row, autocommit=False) as conn:
            db.try_admit_admin_login(
                conn,
                limiter_keys=(limiter_key,),
                now=now,
                rate_limit=5,
                window_seconds=900,
                lockout_seconds=900,
            )
            conn.commit()

            with conn.cursor() as cur:
                cur.execute(
                    "SELECT limiter_key FROM admin_login_rate_limits WHERE limiter_key = %s",
                    (limiter_key,),
                )
                row = cur.fetchone()
            assert row is not None
            assert row["limiter_key"] == limiter_key
            assert len(row["limiter_key"]) == 64
            assert candidate not in row["limiter_key"]
            assert source not in row["limiter_key"]

            scope = {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/admin/login",
                "raw_path": b"/admin/login",
                "query_string": b"",
                "headers": [(b"x-request-id", b"corr-persist-242")],
                "client": ("203.0.113.200", 12345),
                "server": ("testserver", 80),
            }
            request = Request(scope)
            actor_context = anonymous_actor_context(request)

            with crm_transaction(conn):
                audit_service.record_login_failure(
                    conn,
                    actor_context=actor_context,
                    reason="invalid_credentials",
                    repository=audit_repo,
                )
            conn.commit()

            with conn.cursor() as cur:
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
            serialized = repr(dict(audit_row))
            assert candidate not in serialized
    finally:
        monkeypatch.undo()
        with psycopg.connect(database_url, autocommit=False) as cleanup:
            cleanup.execute("DROP SCHEMA IF EXISTS public CASCADE")
            cleanup.execute("CREATE SCHEMA public")
            cleanup.commit()
