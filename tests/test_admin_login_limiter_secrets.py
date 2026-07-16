"""Tests for keyed admin login limiter identifiers and anonymous failure actors."""

from __future__ import annotations

import json
import logging
import os
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Generator, Iterator
from unittest.mock import MagicMock, patch

import psycopg
import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from app import admin_auth, audit_service, db
from app.admin_security import (
    LIMITER_DOMAIN_ACCOUNT,
    LIMITER_DOMAIN_SOURCE,
    AdminSecurityConfigError,
    digest_limiter_key,
    unkeyed_sha256_limiter_identifier,
    validate_admin_security_config,
)
from app.config import Settings, get_settings
from app.crm_uow import crm_transaction
from app.main import app
from app.migrations.runner import apply_migrations
from tests.conftest import TEST_LIMITER_SECRET

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SESSION_SECRET = "test-session-secret-32chars-minimum"
OTHER_LIMITER_SECRET = "other-limiter-secret-32chars-minimum!"
PREVIOUS_LIMITER_SECRET = "previous-limiter-secret-32chars-minimum"

client = TestClient(app, follow_redirects=False)

_REQUIRED = (os.environ.get("REQUIRE_TEST_DATABASE") or "").strip() in {"1", "true", "yes"}
_DATABASE_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip()


def _admin_settings(**overrides: str) -> Settings:
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
        "admin_login_limiter_previous_secret": "",
    }
    base.update(overrides)
    return Settings(**base)


@pytest.fixture(autouse=True)
def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SESSION_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    admin_auth.reset_login_rate_limiter()


def _require_database_url() -> str:
    if _DATABASE_URL:
        return _DATABASE_URL
    if _REQUIRED:
        pytest.fail("REQUIRE_TEST_DATABASE=1 but TEST_DATABASE_URL is unset")
    pytest.skip("TEST_DATABASE_URL not set; skipping live Postgres limiter tests")


@pytest.fixture(scope="module")
def database_url() -> str:
    return _require_database_url()


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
def pg_conn(database_url: str) -> Iterator[psycopg.Connection]:
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


@contextmanager
def mock_db_connection() -> Generator[MagicMock, None, None]:
    conn = MagicMock()
    with patch("app.admin_routes.db.db_connection") as db_conn:
        db_conn.return_value.__enter__.return_value = conn
        db_conn.return_value.__exit__.return_value = None
        yield conn


def _extract_csrf_token(html: str) -> str:
    import re

    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match is not None
    return match.group(1)


@pytest.mark.unit
def test_limiter_identifier_is_not_plain_sha256() -> None:
    settings = _admin_settings()
    source = "203.0.113.10"
    account = "operator"
    keyed_source = admin_auth.build_source_rate_limit_key(source, settings=settings)
    keyed_account = admin_auth.build_account_rate_limit_key(account, settings=settings)
    plain_source = unkeyed_sha256_limiter_identifier(LIMITER_DOMAIN_SOURCE, source)
    plain_account = unkeyed_sha256_limiter_identifier(LIMITER_DOMAIN_ACCOUNT, account)
    assert keyed_source != plain_source
    assert keyed_account != plain_account
    assert len(keyed_source) == 64


@pytest.mark.unit
def test_limiter_identifier_depends_on_secret() -> None:
    left = digest_limiter_key(
        secret=TEST_LIMITER_SECRET,
        domain=LIMITER_DOMAIN_SOURCE,
        material="203.0.113.10",
    )
    right = digest_limiter_key(
        secret=OTHER_LIMITER_SECRET,
        domain=LIMITER_DOMAIN_SOURCE,
        material="203.0.113.10",
    )
    assert left != right


@pytest.mark.unit
def test_limiter_identifier_is_stable_for_same_inputs() -> None:
    material = "203.0.113.10"
    first = digest_limiter_key(
        secret=TEST_LIMITER_SECRET,
        domain=LIMITER_DOMAIN_SOURCE,
        material=material,
    )
    second = digest_limiter_key(
        secret=TEST_LIMITER_SECRET,
        domain=LIMITER_DOMAIN_SOURCE,
        material=material,
    )
    assert first == second


@pytest.mark.unit
def test_limiter_identifier_domain_separation() -> None:
    payload = "203.0.113.10"
    source_key = digest_limiter_key(
        secret=TEST_LIMITER_SECRET,
        domain=LIMITER_DOMAIN_SOURCE,
        material=payload,
    )
    account_key = digest_limiter_key(
        secret=TEST_LIMITER_SECRET,
        domain=LIMITER_DOMAIN_ACCOUNT,
        material=payload,
    )
    assert source_key != account_key


@pytest.mark.unit
@pytest.mark.parametrize(
    ("secret_value", "message"),
    [
        ("", "required"),
        ("short-secret", "at least"),
        ("changeme000000000000000000000000", "placeholder"),
        ("a" * 40, "too weak"),
    ],
)
def test_limiter_secret_validation_rejects_weak_material(
    secret_value: str,
    message: str,
) -> None:
    settings = _admin_settings(admin_login_limiter_secret=secret_value)
    with pytest.raises(AdminSecurityConfigError, match=message):
        validate_admin_security_config(settings)


@pytest.mark.unit
def test_previous_limiter_secret_must_differ_from_current() -> None:
    settings = _admin_settings(
        admin_login_limiter_previous_secret=TEST_LIMITER_SECRET,
    )
    with pytest.raises(AdminSecurityConfigError, match="must differ"):
        validate_admin_security_config(settings)


@pytest.mark.integration
def test_rotation_guard_key_blocks_without_incrementing_current_bucket(
    pg_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_PREVIOUS_SECRET", PREVIOUS_LIMITER_SECRET)
    settings = get_settings()
    now = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    legacy_key = digest_limiter_key(
        secret=PREVIOUS_LIMITER_SECRET,
        domain=LIMITER_DOMAIN_SOURCE,
        material="203.0.113.88",
    )
    current_key = admin_auth.build_source_rate_limit_key("203.0.113.88", settings=settings)
    assert legacy_key != current_key

    with pg_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO admin_login_rate_limits (
                limiter_key, failure_count, window_started_at, locked_until, updated_at
            )
            VALUES (%s, 5, %s, %s, %s)
            """,
            (
                legacy_key,
                now,
                now + timedelta(seconds=900),
                now,
            ),
        )
        pg_conn.commit()

    admission = db.try_admit_admin_login(
        pg_conn,
        limiter_keys=(current_key,),
        guard_keys=(legacy_key,),
        now=now + timedelta(seconds=1),
        rate_limit=5,
        window_seconds=900,
        lockout_seconds=900,
    )
    assert not admission.admitted
    assert admission.already_locked

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT failure_count FROM admin_login_rate_limits WHERE limiter_key = %s",
            (current_key,),
        )
        row = cur.fetchone()
    assert row is None


@pytest.mark.integration
def test_rotation_cleanup_removes_stale_previous_key_rows(pg_conn: psycopg.Connection) -> None:
    now = datetime(2026, 7, 2, 12, 0, tzinfo=timezone.utc)
    stale_key = digest_limiter_key(
        secret=PREVIOUS_LIMITER_SECRET,
        domain=LIMITER_DOMAIN_SOURCE,
        material="203.0.113.89",
    )
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
        window_seconds=60,
        lockout_seconds=60,
    )
    assert deleted >= 1
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS count FROM admin_login_rate_limits WHERE limiter_key = %s",
            (stale_key,),
        )
        row = cur.fetchone()
    assert row is not None
    assert int(row["count"]) == 0


@pytest.mark.unit
@pytest.mark.integration
def test_unknown_username_failure_audit_is_anonymous() -> None:
    admitted = admin_auth.LoginAdmissionResult(
        admitted=True,
        throttled=False,
        already_locked=False,
        lockout_transition=False,
    )

    with mock_db_connection():
        with (
            patch("app.admin_routes.db.create_admin_login_flow", return_value=1),
            patch("app.admin_routes.db.claim_admin_login_flow", return_value={"id": 1}),
            patch("app.admin_routes.admin_auth.try_admit_login_attempt", return_value=admitted),
            patch("app.admin_routes.audit_service.record_login_failure") as failure_spy,
        ):
            form = client.get("/admin/login")
            csrf = _extract_csrf_token(form.text)
            response = client.post(
                "/admin/login",
                data={
                    "username": "attacker-supplied-name",
                    "password": "wrong-password",
                    "csrf_token": csrf,
                },
                cookies=dict(form.cookies),
            )
            assert response.status_code == 401
            failure_spy.assert_called_once()
            actor_context = failure_spy.call_args.kwargs["actor_context"]
            assert actor_context.actor == "anonymous"
            assert failure_spy.call_args.kwargs["reason"] == "invalid_credentials"
            assert "attacker-supplied-name" not in failure_spy.call_args.kwargs["reason"]


@pytest.mark.unit
@pytest.mark.integration
def test_configured_username_wrong_password_keeps_anonymous_actor() -> None:
    captured: dict[str, Any] = {}
    admitted = admin_auth.LoginAdmissionResult(
        admitted=True,
        throttled=False,
        already_locked=False,
        lockout_transition=False,
    )

    def _capture(_conn: MagicMock, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"id": "evt"}

    with mock_db_connection():
        with (
            patch("app.admin_routes.db.create_admin_login_flow", return_value=1),
            patch("app.admin_routes.db.claim_admin_login_flow", return_value={"id": 1}),
            patch("app.admin_routes.admin_auth.try_admit_login_attempt", return_value=admitted),
            patch("app.admin_routes.audit_service.record_login_failure", side_effect=_capture),
        ):
            form = client.get("/admin/login")
            csrf = _extract_csrf_token(form.text)
            response = client.post(
                "/admin/login",
                data={
                    "username": TEST_USERNAME,
                    "password": "wrong-password",
                    "csrf_token": csrf,
                },
                cookies=dict(form.cookies),
            )
            assert response.status_code == 401
            assert captured["actor_context"].actor == "anonymous"
            assert captured["reason"] == "invalid_credentials"


@pytest.mark.unit
@pytest.mark.integration
def test_invalid_flow_audit_is_anonymous() -> None:
    captured: dict[str, Any] = {}
    admitted = admin_auth.LoginAdmissionResult(
        admitted=True,
        throttled=False,
        already_locked=False,
        lockout_transition=False,
    )

    def _capture(_conn: MagicMock, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"id": "evt"}

    with mock_db_connection():
        with (
            patch("app.admin_routes.db.create_admin_login_flow", return_value=1),
            patch("app.admin_routes.db.claim_admin_login_flow", return_value=None),
            patch("app.admin_routes.admin_auth.try_admit_login_attempt", return_value=admitted),
            patch("app.admin_routes.audit_service.record_login_failure", side_effect=_capture),
        ):
            form = client.get("/admin/login")
            csrf = _extract_csrf_token(form.text)
            response = client.post(
                "/admin/login",
                data={
                    "username": "candidate-user",
                    "password": "wrong-password",
                    "csrf_token": csrf,
                },
                cookies=dict(form.cookies),
            )
            assert response.status_code == 400
            assert captured["actor_context"].actor == "anonymous"
            assert captured["reason"] == "invalid_csrf"


@pytest.mark.unit
@pytest.mark.integration
def test_successful_login_retains_authenticated_actor() -> None:
    with mock_db_connection():
        with patch("app.admin_routes._try_claim_login_flow", return_value=True):
            with patch("app.admin_routes.db.create_admin_session", return_value=42):
                with patch("app.admin_routes.audit_service.record_login_success") as success_audit:
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
                    assert success_audit.call_args.kwargs["session_id"] == 42


@pytest.mark.unit
@pytest.mark.integration
def test_failed_login_logs_exclude_candidate_and_limiter_material(
    caplog: pytest.LogCaptureFixture,
) -> None:
    candidate = "attacker-log-candidate"
    admitted = admin_auth.LoginAdmissionResult(
        admitted=True,
        throttled=False,
        already_locked=False,
        lockout_transition=False,
    )
    with mock_db_connection():
        with (
            patch("app.admin_routes.db.create_admin_login_flow", return_value=1),
            patch("app.admin_routes.db.claim_admin_login_flow", return_value={"id": 1}),
            patch("app.admin_routes.admin_auth.try_admit_login_attempt", return_value=admitted),
            patch("app.admin_routes.audit_service.record_login_failure", return_value={"id": "evt"}),
        ):
            with caplog.at_level(logging.INFO):
                form = client.get("/admin/login")
                csrf = _extract_csrf_token(form.text)
                response = client.post(
                    "/admin/login",
                    data={
                        "username": candidate,
                        "password": "wrong-password",
                        "csrf_token": csrf,
                    },
                    cookies=dict(form.cookies),
                )
                assert response.status_code == 401

    combined = caplog.text
    assert candidate not in combined
    assert TEST_LIMITER_SECRET not in combined
    assert TEST_SESSION_SECRET not in combined
    plain = unkeyed_sha256_limiter_identifier(LIMITER_DOMAIN_SOURCE, "testclient")
    assert plain not in combined


@pytest.mark.integration
def test_postgres_persists_keyed_limiter_identifier_and_anonymous_actor(
    pg_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "5")
    settings = get_settings()
    source = "203.0.113.200"
    source_key = admin_auth.build_source_rate_limit_key(source, settings=settings)
    plain = unkeyed_sha256_limiter_identifier(LIMITER_DOMAIN_SOURCE, source.lower())
    assert source_key != plain

    now = datetime(2026, 7, 3, 12, 0, tzinfo=timezone.utc)
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

    repo = MagicMock()
    repo.append.return_value = {"id": "evt-pg"}
    admitted = admin_auth.LoginAdmissionResult(
        admitted=True,
        throttled=False,
        already_locked=False,
        lockout_transition=False,
    )
    real_record = audit_service.record_login_failure

    @contextmanager
    def _pg_connection(_url: str) -> Iterator[psycopg.Connection]:
        yield pg_conn

    with ExitStack() as stack:
        stack.enter_context(patch("app.admin_routes.db.db_connection", side_effect=_pg_connection))
        stack.enter_context(patch("app.admin_auth.db.db_connection", side_effect=_pg_connection))
        stack.enter_context(patch("app.admin_routes.crm_transaction", side_effect=crm_transaction))
        stack.enter_context(patch("app.admin_routes.db.create_admin_login_flow", return_value=1))
        stack.enter_context(
            patch("app.admin_routes.db.claim_admin_login_flow", return_value={"id": 1})
        )
        stack.enter_context(
            patch("app.admin_routes.admin_auth.try_admit_login_attempt", return_value=admitted)
        )
        stack.enter_context(
            patch(
                "app.admin_routes.audit_service.record_login_failure",
                side_effect=lambda conn, **kwargs: real_record(
                    conn, repository=repo, **kwargs
                ),
            )
        )
        form = client.get("/admin/login")
        csrf = _extract_csrf_token(form.text)
        response = client.post(
            "/admin/login",
            data={
                "username": "persisted-candidate",
                "password": "wrong-password",
                "csrf_token": csrf,
            },
            cookies=dict(form.cookies),
        )
        assert response.status_code == 401

    appended = repo.append.call_args.kwargs
    assert appended["actor"] == "anonymous"
    assert "persisted-candidate" not in json.dumps(appended)
