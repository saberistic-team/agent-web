"""Tests for keyed admin login limiter identifiers and anonymous failure actors."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Generator
from unittest.mock import MagicMock, patch

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from app import admin_auth, audit_service, db
from app.actor_context import anonymous_actor_context
from app.config import get_settings
from app.crm_uow import crm_transaction
from app.main import app

from tests.conftest import TEST_LIMITER_SECRET
from tests.test_admin_auth import (
    TEST_HASH,
    TEST_PASSWORD,
    TEST_SECRET,
    TEST_USERNAME,
    FakeRateLimitStore,
    mock_db_connection,
    shared_rate_limiter,
    _fetch_login_form,
    _login,
)

client = TestClient(app, follow_redirects=False)

TEST_LIMITER_SECRET_B = "other-limiter-secret-32chars-minimum!!"
TEST_SOURCE = "203.0.113.42"
TEST_ATTACKER_USERNAME = "attacker-candidate@example.com"


@pytest.fixture
def rate_limit_store() -> FakeRateLimitStore:
    return FakeRateLimitStore()


@pytest.fixture(autouse=True)
def _admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import admin_auth as admin_auth_module
    from tests.test_admin_auth import _login_flows, _session_store

    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "5")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_WINDOW_SECONDS", "900")
    monkeypatch.setenv("ADMIN_LOGIN_LOCKOUT_SECONDS", "900")
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    monkeypatch.delenv("ADMIN_TRUSTED_EDGE_CIDRS", raising=False)
    admin_auth_module.reset_login_rate_limiter()
    _login_flows.clear()
    _session_store.clear()


def _plain_sha256_limiter_key(prefix: str, material: str) -> str:
    payload = f"{prefix}:{material}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _settings_with_secrets(
    monkeypatch: pytest.MonkeyPatch,
    *,
    current: str = TEST_LIMITER_SECRET,
    previous: str = "",
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", current)
    if previous:
        monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", previous)
    else:
        monkeypatch.delenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", raising=False)


@pytest.mark.unit
def test_limiter_identifier_is_not_plain_sha256(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings_with_secrets(monkeypatch)
    settings = get_settings()
    keyed = admin_auth.build_source_rate_limit_key(TEST_SOURCE, settings)
    plain_src = _plain_sha256_limiter_key("src", TEST_SOURCE.lower())
    plain_acct = _plain_sha256_limiter_key("acct", TEST_USERNAME.lower())
    assert keyed != plain_src
    assert admin_auth.build_account_rate_limit_key(TEST_USERNAME, settings) != plain_acct


@pytest.mark.unit
def test_limiter_identifier_depends_on_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings_with_secrets(monkeypatch, current=TEST_LIMITER_SECRET)
    settings_a = get_settings()
    key_a = admin_auth.build_source_rate_limit_key(TEST_SOURCE, settings_a)

    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET_B)
    settings_b = get_settings()
    key_b = admin_auth.build_source_rate_limit_key(TEST_SOURCE, settings_b)

    assert key_a != key_b


@pytest.mark.unit
def test_limiter_identifier_is_stable_across_settings_reload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings_with_secrets(monkeypatch)
    first = admin_auth.build_source_rate_limit_key(TEST_SOURCE, get_settings())
    second = admin_auth.build_source_rate_limit_key(TEST_SOURCE, get_settings())
    assert first == second
    assert len(first) == 64


@pytest.mark.unit
def test_limiter_identifier_domain_separation(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings_with_secrets(monkeypatch)
    settings = get_settings()
    shared_material = "203.0.113.1"
    source_key = admin_auth.build_source_rate_limit_key(shared_material, settings)
    account_key = admin_auth.build_account_rate_limit_key(shared_material, settings)
    assert source_key != account_key


@pytest.mark.unit
@pytest.mark.parametrize(
    ("secret", "label"),
    [
        ("", "ADMIN_LOGIN_LIMITER_SECRET"),
        ("short", "ADMIN_LOGIN_LIMITER_SECRET"),
        ("changeme", "ADMIN_LOGIN_LIMITER_SECRET"),
        ("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "ADMIN_LOGIN_LIMITER_SECRET"),
    ],
)
def test_limiter_secret_validation_rejects_weak_or_missing(
    secret: str,
    label: str,
) -> None:
    with pytest.raises(ValueError, match=label):
        admin_auth.validate_admin_login_limiter_secret_value(secret, label=label)


@pytest.mark.unit
def test_limiter_secret_validation_rejects_identical_previous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings_with_secrets(monkeypatch, current=TEST_LIMITER_SECRET, previous=TEST_LIMITER_SECRET)
    with pytest.raises(ValueError, match="must differ"):
        admin_auth.validate_admin_login_limiter_secrets(get_settings())


@pytest.mark.unit
def test_startup_validation_fails_when_limiter_secret_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ADMIN_LOGIN_LIMITER_SECRET", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    with pytest.raises(ValueError, match="ADMIN_LOGIN_LIMITER_SECRET"):
        admin_auth.validate_admin_login_limiter_secrets(get_settings())


@pytest.mark.unit
def test_rotation_includes_previous_key_variants(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings_with_secrets(
        monkeypatch,
        current=TEST_LIMITER_SECRET,
        previous=TEST_LIMITER_SECRET_B,
    )
    settings = get_settings()
    keys = admin_auth.login_limiter_keys(
        submitted_username=TEST_USERNAME,
        client_source=TEST_SOURCE,
        configured_admin_username=TEST_USERNAME,
        settings=settings,
    )
    current_source = admin_auth.build_source_rate_limit_key(TEST_SOURCE, settings)
    previous_source = admin_auth._digest_limiter_key(
        TEST_LIMITER_SECRET_B,
        admin_auth.LIMITER_KEY_DOMAIN_SRC,
        TEST_SOURCE,
    )
    assert current_source in keys
    assert previous_source in keys
    assert len(keys) == 4


@pytest.mark.unit
def test_rotation_cleanup_removes_stale_previous_key_rows() -> None:
    store = FakeRateLimitStore()
    previous_key = admin_auth._digest_limiter_key(
        TEST_LIMITER_SECRET_B,
        admin_auth.LIMITER_KEY_DOMAIN_SRC,
        "203.0.113.66",
    )
    now = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
    store.rows[previous_key] = {
        "failure_count": 1,
        "window_started_at": now,
        "locked_until": None,
        "updated_at": now,
    }
    deleted = store.cleanup(
        now + timedelta(seconds=200),
        window_seconds=60,
        lockout_seconds=60,
    )
    assert deleted >= 1
    assert previous_key not in store.rows


@pytest.mark.unit
@pytest.mark.integration
def test_unknown_username_failure_records_anonymous_actor(
    rate_limit_store: FakeRateLimitStore,
) -> None:
    captured: list[dict[str, Any]] = []

    def _capture(
        conn: Any,
        *,
        actor_context: Any,
        reason: str,
        repository: Any = None,
    ) -> dict[str, Any]:
        captured.append(
            {
                "actor": actor_context.actor,
                "reason": reason,
                "correlation_id": actor_context.correlation_id,
            }
        )
        return {"id": "evt"}

    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                with patch(
                    "app.admin_routes.audit_service.record_login_failure",
                    side_effect=_capture,
                ):
                    response = _login(username=TEST_ATTACKER_USERNAME, password="wrong")
    assert response.status_code == 401
    assert len(captured) == 1
    assert captured[0]["actor"] == "anonymous"
    assert TEST_ATTACKER_USERNAME not in json.dumps(captured[0])


@pytest.mark.unit
@pytest.mark.integration
def test_configured_username_wrong_password_actor_remains_anonymous(
    rate_limit_store: FakeRateLimitStore,
) -> None:
    repo = MagicMock()
    repo.append.return_value = {"id": "evt"}

    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                with patch(
                    "app.admin_routes.audit_service.record_login_failure",
                    wraps=audit_service.record_login_failure,
                ) as failure_audit:
                    with patch(
                        "app.audit_service.get_repositories"
                    ) as get_repos:
                        get_repos.return_value.audit_events = repo
                        response = _login(password="wrong-password")
    assert response.status_code == 401
    failure_audit.assert_called_once()
    actor_context = failure_audit.call_args.kwargs["actor_context"]
    assert actor_context.actor == "anonymous"
    append_kwargs = repo.append.call_args.kwargs
    event_blob = json.dumps(append_kwargs)
    assert TEST_USERNAME not in append_kwargs.get("actor", "")
    assert TEST_USERNAME not in event_blob


@pytest.mark.unit
@pytest.mark.integration
def test_invalid_csrf_failure_actor_is_anonymous(
    rate_limit_store: FakeRateLimitStore,
) -> None:
    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            with patch("app.admin_routes._try_claim_login_flow", return_value=False):
                with patch(
                    "app.admin_routes.audit_service.record_login_failure"
                ) as failure_audit:
                    csrf_token, cookies = _fetch_login_form()
                    response = client.post(
                        "/admin/login",
                        data={
                            "username": TEST_ATTACKER_USERNAME,
                            "password": TEST_PASSWORD,
                            "csrf_token": "tampered-token",
                        },
                        cookies=cookies,
                    )
    assert response.status_code == 400
    failure_audit.assert_called_once()
    assert failure_audit.call_args.kwargs["actor_context"].actor == "anonymous"
    assert failure_audit.call_args.kwargs["reason"] == "invalid_csrf"


@pytest.mark.unit
@pytest.mark.integration
def test_lockout_transition_actor_is_anonymous(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                with patch(
                    "app.admin_routes.audit_service.record_login_failure"
                ) as failure_audit:
                    assert _login(password="wrong").status_code == 401
                    lockout = _login(password="wrong")
                    assert lockout.status_code == 401
                    assert failure_audit.call_args.kwargs["actor_context"].actor == "anonymous"
                    assert failure_audit.call_args.kwargs["reason"] == "rate_limited"


@pytest.mark.unit
@pytest.mark.integration
def test_successful_login_retains_administrator_actor(
    rate_limit_store: FakeRateLimitStore,
) -> None:
    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                with patch(
                    "app.admin_routes.db.create_admin_session", return_value=42
                ):
                    with patch(
                        "app.admin_routes.audit_service.record_login_success"
                    ) as success_audit:
                        response = _login()
    assert response.status_code == 303
    success_audit.assert_called_once()
    assert success_audit.call_args.kwargs["actor_context"].actor == TEST_USERNAME
    assert success_audit.call_args.kwargs["session_id"] is not None


@pytest.mark.unit
@pytest.mark.integration
def test_failed_login_logs_exclude_candidate_and_digest_inputs(
    rate_limit_store: FakeRateLimitStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                response = _login(username=TEST_ATTACKER_USERNAME, password="wrong")
    assert response.status_code == 401
    log_blob = caplog.text
    assert TEST_ATTACKER_USERNAME not in log_blob
    assert TEST_LIMITER_SECRET not in log_blob
    assert "src:" not in log_blob
    assert "acct:" not in log_blob


def _require_database_url() -> str:
    import os

    required = (os.environ.get("REQUIRE_TEST_DATABASE") or "").strip() in {"1", "true", "yes"}
    database_url = (os.environ.get("TEST_DATABASE_URL") or "").strip()
    if database_url:
        return database_url
    if required:
        pytest.fail("REQUIRE_TEST_DATABASE=1 but TEST_DATABASE_URL is unset")
    pytest.skip("TEST_DATABASE_URL not set; skipping live Postgres login limiter tests")


@pytest.fixture
def database_url() -> str:
    return _require_database_url()


@pytest.fixture
def pg_conn(database_url: str) -> Generator[psycopg.Connection, None, None]:
    from app.migrations.runner import apply_migrations

    def _reset_public_schema(conn: psycopg.Connection) -> None:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("GRANT ALL ON SCHEMA public TO CURRENT_USER")
        conn.execute("GRANT ALL ON SCHEMA public TO public")
        conn.commit()

    with psycopg.connect(database_url, autocommit=False) as bootstrap:
        _reset_public_schema(bootstrap)
        apply_migrations(bootstrap)
    conn = psycopg.connect(database_url, row_factory=dict_row, autocommit=False)
    try:
        yield conn
    finally:
        conn.rollback()
        with psycopg.connect(database_url, autocommit=False) as cleanup:
            _reset_public_schema(cleanup)


@pytest.mark.integration
def test_postgres_persists_keyed_limiter_and_anonymous_actor(
    pg_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings_with_secrets(monkeypatch)
    settings = get_settings()
    source_key = admin_auth.build_source_rate_limit_key(TEST_SOURCE, settings)
    plain = _plain_sha256_limiter_key("src", TEST_SOURCE.lower())
    now = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
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
    assert row["limiter_key"] == source_key
    assert row["limiter_key"] != plain
    assert len(row["limiter_key"]) == 64

    repo = MagicMock()
    repo.append.return_value = {"id": "evt-1"}
    request = MagicMock()
    request.headers = {}
    request.state = MagicMock()
    request.state.correlation_id = "trace-242"
    actor_context = anonymous_actor_context(request)
    with crm_transaction(pg_conn):
        audit_service.record_login_failure(
            pg_conn,
            actor_context=actor_context,
            reason="invalid_credentials",
            repository=repo,
        )
    append_kwargs = repo.append.call_args.kwargs
    assert append_kwargs["actor"] == "anonymous"
    assert TEST_ATTACKER_USERNAME not in json.dumps(append_kwargs)
