"""Tests for HMAC login limiter identifiers and anonymous failure audit actors."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import psycopg
import pytest
from fastapi.testclient import TestClient

from app import admin_auth, audit_service, db
from app.actor_context import anonymous_actor_context
from app.admin_auth import LOGIN_FLOW_COOKIE_NAME, SESSION_COOKIE_NAME
from app.config import Settings, get_settings
from app.crm_uow import crm_transaction
from app.main import app
from tests.conftest import TEST_LIMITER_SECRET
from tests.test_admin_auth import (
    FakeRateLimitStore,
    TEST_HASH,
    TEST_PASSWORD,
    TEST_SECRET,
    TEST_USERNAME,
    mock_db_connection,
    shared_rate_limiter,
)


@pytest.fixture
def rate_limit_store() -> FakeRateLimitStore:
    return FakeRateLimitStore()

client = TestClient(app, follow_redirects=False)

ALT_LIMITER_SECRET = "alternate-limiter-secret-32chars-min!!"
PREVIOUS_LIMITER_SECRET = "previous-limiter-secret-32chars-min!!"


def _settings(**overrides: str) -> Settings:
    base = {
        "DATABASE_URL": "postgresql://test:test@localhost:5432/test",
        "ADMIN_USERNAME": TEST_USERNAME,
        "ADMIN_PASSWORD_HASH": TEST_HASH,
        "ADMIN_SESSION_SECRET": TEST_SECRET,
        "ADMIN_LOGIN_LIMITER_SECRET": TEST_LIMITER_SECRET,
        "BASE_URL": "http://testserver",
    }
    base.update(overrides)
    return Settings(
        database_url=base["DATABASE_URL"],
        stripe_secret_key="",
        stripe_webhook_secret="",
        stripe_publishable_key="",
        resend_api_key="",
        from_email="noreply@test",
        notify_email="inbox@test",
        base_url=base["BASE_URL"],
        plausible_domain="",
        plausible_api_key="",
        analytics_environment="test",
        admin_username=base["ADMIN_USERNAME"],
        admin_password_hash=base["ADMIN_PASSWORD_HASH"],
        admin_session_secret=base["ADMIN_SESSION_SECRET"],
        admin_login_limiter_secret=base.get("ADMIN_LOGIN_LIMITER_SECRET", ""),
        admin_login_limiter_previous_secret=base.get(
            "ADMIN_LOGIN_LIMITER_PREVIOUS_SECRET", ""
        ),
    )


def _plain_sha256_limiter_key(prefix: str, material: str) -> str:
    payload = f"{prefix}:{material.strip().lower()}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@pytest.fixture
def limiter_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    admin_auth.reset_login_rate_limiter()


@pytest.mark.unit
def test_limiter_identifier_is_not_plain_sha256(limiter_env: None) -> None:
    settings = get_settings()
    source_key = admin_auth.build_source_rate_limit_key("203.0.113.1", settings)
    account_key = admin_auth.build_account_rate_limit_key("operator", settings)
    assert source_key != _plain_sha256_limiter_key("src", "203.0.113.1")
    assert account_key != _plain_sha256_limiter_key("acct", "operator")


@pytest.mark.unit
def test_limiter_identifier_depends_on_secret(limiter_env: None) -> None:
    settings_a = _settings(ADMIN_LOGIN_LIMITER_SECRET=TEST_LIMITER_SECRET)
    settings_b = _settings(ADMIN_LOGIN_LIMITER_SECRET=ALT_LIMITER_SECRET)
    key_a = admin_auth.build_source_rate_limit_key("203.0.113.1", settings_a)
    key_b = admin_auth.build_source_rate_limit_key("203.0.113.1", settings_b)
    assert key_a != key_b


@pytest.mark.unit
def test_limiter_identifier_stable_for_same_inputs(limiter_env: None) -> None:
    settings = get_settings()
    first = admin_auth.build_source_rate_limit_key("203.0.113.1", settings)
    second = admin_auth.build_source_rate_limit_key("203.0.113.1", settings)
    assert first == second
    assert len(first) == 64


@pytest.mark.unit
def test_limiter_domain_separation(limiter_env: None) -> None:
    settings = get_settings()
    shared_material = "203.0.113.1"
    source_key = admin_auth.build_source_rate_limit_key(shared_material, settings)
    account_key = admin_auth.build_account_rate_limit_key(shared_material, settings)
    assert source_key != account_key


@pytest.mark.unit
@pytest.mark.parametrize(
    ("secret", "field"),
    [
        ("", "ADMIN_LOGIN_LIMITER_SECRET"),
        ("short", "ADMIN_LOGIN_LIMITER_SECRET"),
        ("changeme", "ADMIN_LOGIN_LIMITER_SECRET"),
        ("your-secret-here-please-replace", "ADMIN_LOGIN_LIMITER_SECRET"),
    ],
)
def test_limiter_secret_validation_rejects_weak_or_missing(
    limiter_env: None,
    secret: str,
    field: str,
) -> None:
    settings = _settings(ADMIN_LOGIN_LIMITER_SECRET=secret)
    with pytest.raises(ValueError, match=field):
        admin_auth.validate_admin_login_limiter_secrets(settings)


@pytest.mark.unit
def test_limiter_previous_secret_must_differ_from_current(limiter_env: None) -> None:
    settings = _settings(
        ADMIN_LOGIN_LIMITER_SECRET=TEST_LIMITER_SECRET,
        ADMIN_LOGIN_LIMITER_PREVIOUS_SECRET=TEST_LIMITER_SECRET,
    )
    with pytest.raises(ValueError, match="must differ"):
        admin_auth.validate_admin_login_limiter_secrets(settings)


@pytest.mark.unit
def test_startup_validates_limiter_secret(limiter_env: None) -> None:
    import asyncio

    from app.main import lifespan

    class _App:
        pass

    with (
        patch("app.main.db.init_db"),
        patch("app.main.get_settings") as settings_mock,
    ):
        settings_mock.return_value = _settings(ADMIN_LOGIN_LIMITER_SECRET="short")

        async def _run() -> None:
            async with lifespan(_App()):
                pass

        with pytest.raises(ValueError, match="ADMIN_LOGIN_LIMITER_SECRET"):
            asyncio.run(_run())


@pytest.mark.unit
def test_lockout_transition_audit_is_anonymous(
    limiter_env: None,
    rate_limit_store: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    captured_reasons: list[str] = []
    captured_actors: list[str] = []

    def _capture(request: Any, *, reason: str) -> None:
        captured_reasons.append(reason)
        captured_actors.append(anonymous_actor_context(request).actor)

    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                with patch("app.admin_routes._record_login_failure", side_effect=_capture):
                    assert client.post(
                        "/admin/login",
                        data={
                            "username": TEST_USERNAME,
                            "password": "wrong",
                            "csrf_token": "flow-csrf",
                        },
                    ).status_code == 401
                    assert client.post(
                        "/admin/login",
                        data={
                            "username": TEST_USERNAME,
                            "password": "wrong",
                            "csrf_token": "flow-csrf",
                        },
                    ).status_code == 401
    assert captured_reasons[-1] == "rate_limited"
    assert captured_actors[-1] == "anonymous"


@pytest.mark.unit
def test_rotation_includes_previous_secret_variants(limiter_env: None) -> None:
    settings = _settings(
        ADMIN_LOGIN_LIMITER_SECRET=TEST_LIMITER_SECRET,
        ADMIN_LOGIN_LIMITER_PREVIOUS_SECRET=PREVIOUS_LIMITER_SECRET,
    )
    keys = admin_auth.limiter_keys_for_domain(
        admin_auth.LIMITER_DOMAIN_SOURCE,
        "203.0.113.9",
        settings,
    )
    assert len(keys) == 2
    assert keys[0] != keys[1]
    assert keys[0] == admin_auth.build_source_rate_limit_key("203.0.113.9", settings)


@pytest.mark.unit
def test_rotation_cleanup_removes_previous_secret_rows(
    rate_limit_store: Any,
) -> None:
    settings = _settings(
        ADMIN_LOGIN_LIMITER_SECRET=TEST_LIMITER_SECRET,
        ADMIN_LOGIN_LIMITER_PREVIOUS_SECRET=PREVIOUS_LIMITER_SECRET,
    )
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    old_key = admin_auth.limiter_keys_for_domain(
        admin_auth.LIMITER_DOMAIN_SOURCE,
        "203.0.113.55",
        settings,
    )[1]
    rate_limit_store.rows[old_key] = {
        "failure_count": 1,
        "window_started_at": now - timedelta(days=30),
        "locked_until": None,
        "updated_at": now - timedelta(days=30),
    }
    deleted = rate_limit_store.cleanup(
        now,
        window_seconds=900,
        lockout_seconds=900,
    )
    assert deleted == 1
    assert old_key not in rate_limit_store.rows


@pytest.mark.unit
def test_unknown_username_failure_audit_uses_anonymous_actor(
    limiter_env: None,
    rate_limit_store: Any,
) -> None:
    repo = MagicMock()
    repo.append.return_value = {"id": "evt-1"}

    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                with patch(
                    "app.admin_routes.audit_service.record_login_failure",
                    side_effect=lambda conn, **kwargs: repo.append(**kwargs),
                ) as failure_audit:
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
    event = failure_audit.call_args.kwargs
    assert event["actor_context"].actor == "anonymous"
    assert "attacker-candidate" not in str(event)
    assert "attacker-candidate" not in event["reason"]


@pytest.mark.unit
def test_configured_username_wrong_password_keeps_anonymous_actor(
    limiter_env: None,
    rate_limit_store: Any,
) -> None:
    captured: dict[str, Any] = {}

    def _capture(request: Any, *, reason: str) -> None:
        captured["actor_context"] = anonymous_actor_context(request)
        captured["reason"] = reason

    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                with patch("app.admin_routes._record_login_failure", side_effect=_capture):
                    response = client.post(
                        "/admin/login",
                        data={
                            "username": TEST_USERNAME,
                            "password": "wrong-password",
                            "csrf_token": "flow-csrf",
                        },
                    )
    assert response.status_code == 401
    assert captured["actor_context"].actor == "anonymous"
    assert captured["reason"] == "invalid_credentials"
    assert TEST_USERNAME not in str(captured)


@pytest.mark.unit
def test_invalid_csrf_failure_audit_is_anonymous(
    limiter_env: None,
    rate_limit_store: Any,
) -> None:
    repo = MagicMock()
    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            with patch("app.admin_routes._try_claim_login_flow", return_value=False):
                with patch(
                    "app.admin_routes.audit_service.record_login_failure",
                    side_effect=lambda conn, **kwargs: repo.append(**kwargs),
                ) as failure_audit:
                    response = client.post(
                        "/admin/login",
                        data={
                            "username": TEST_USERNAME,
                            "password": TEST_PASSWORD,
                            "csrf_token": "bad-csrf",
                        },
                    )
    assert response.status_code == 400
    failure_audit.assert_called_once()
    assert failure_audit.call_args.kwargs["actor_context"].actor == "anonymous"


@pytest.mark.unit
def test_successful_login_retains_administrator_actor(
    limiter_env: None,
    rate_limit_store: Any,
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
def test_login_failure_logs_exclude_candidate_and_secret(
    limiter_env: None,
    rate_limit_store: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                client.post(
                    "/admin/login",
                    data={
                        "username": "logged-candidate",
                        "password": "wrong-password",
                        "csrf_token": "flow-csrf",
                    },
                )
    combined = caplog.text
    assert "logged-candidate" not in combined
    assert TEST_LIMITER_SECRET not in combined
    assert "203.0.113" not in combined


@pytest.mark.integration
def test_postgres_limiter_rows_store_hmac_identifiers_and_anonymous_actors() -> None:
    import os
    from contextlib import contextmanager
    from typing import Iterator

    from app.migrations.runner import apply_migrations
    from tests.test_admin_login_rate_limit_integration import _admit, _connect, _reset_public_schema

    url = (os.environ.get("TEST_DATABASE_URL") or "").strip()
    required = (os.environ.get("REQUIRE_TEST_DATABASE") or "").strip() in {"1", "true", "yes"}
    if not url:
        if required:
            pytest.fail("REQUIRE_TEST_DATABASE=1 but TEST_DATABASE_URL is unset")
        pytest.skip("TEST_DATABASE_URL not set")

    with psycopg.connect(url, autocommit=False) as bootstrap:
        _reset_public_schema(bootstrap)
        apply_migrations(bootstrap)
    with _connect(url) as pg_conn:
        try:
            settings = _settings()
            source_key = admin_auth.build_source_rate_limit_key("203.0.113.88", settings)
            assert source_key != _plain_sha256_limiter_key("src", "203.0.113.88")
            now = datetime(2026, 4, 1, tzinfo=timezone.utc)
            admission = _admit(pg_conn, keys=(source_key,), now=now, rate_limit=5)
            assert admission.admitted

            with pg_conn.cursor() as cur:
                cur.execute(
                    "SELECT limiter_key FROM admin_login_rate_limits WHERE limiter_key = %s",
                    (source_key,),
                )
                row = cur.fetchone()
            assert row is not None
            assert row["limiter_key"] == source_key

            repo = MagicMock()
            repo.append.return_value = {"id": "evt-pg"}
            request = MagicMock()
            request.headers = {}
            request.state = MagicMock()
            request.state.correlation_id = "corr-pg"
            actor_context = anonymous_actor_context(request)
            with crm_transaction(pg_conn):
                audit_service.record_login_failure(
                    pg_conn,
                    actor_context=actor_context,
                    reason="invalid_credentials",
                    repository=repo,
                )
            event = repo.append.call_args.kwargs
            assert event["actor_context"].actor == "anonymous"
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
            persisted = json.dumps(
                {
                    "summary_after": audit_row["summary_after"],
                    "metadata": audit_row["metadata"],
                }
            )
            assert TEST_USERNAME not in persisted
        finally:
            pg_conn.rollback()
            with psycopg.connect(url, autocommit=False) as cleanup:
                _reset_public_schema(cleanup)
