"""Tests for keyed admin login limiter identifiers and anonymous failure actors."""

from __future__ import annotations

import hashlib
import logging
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Iterator
from unittest.mock import MagicMock, patch

import psycopg
import pytest
from psycopg.rows import dict_row

from app import admin_auth, audit_service, db
from app.actor_context import anonymous_actor_context
from app.config import Settings, get_settings
from app.main import app
from app.migrations.runner import apply_migrations
from fastapi.testclient import TestClient

from tests.conftest import TEST_LIMITER_SECRET
from tests.test_admin_auth import (
    TEST_PASSWORD,
    TEST_USERNAME,
    mock_db_connection,
    shared_rate_limiter,
    FakeRateLimitStore,
    _fetch_login_form,
    _login,
    _parse_login_form,
)

client = TestClient(app, follow_redirects=False)

_OTHER_LIMITER_SECRET = "other-limiter-secret-32chars-minimum!"
_PREVIOUS_LIMITER_SECRET = "previous-limiter-secret-32chars-minimum"

_REQUIRED = (os.environ.get("REQUIRE_TEST_DATABASE") or "").strip() in {"1", "true", "yes"}
_DATABASE_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip()


@pytest.fixture
def rate_limit_store() -> FakeRateLimitStore:
    store = FakeRateLimitStore()
    admin_auth.reset_login_rate_limiter()
    yield store
    admin_auth.reset_login_rate_limiter()


def _settings_with_secrets(
    *,
    current: str = TEST_LIMITER_SECRET,
    previous: str = "",
) -> Settings:
    base = get_settings()
    return Settings(
        **{
            **base.__dict__,
            "admin_login_limiter_secret": current,
            "admin_login_limiter_previous_secret": previous,
        }
    )


def _plain_sha256_identifier(domain: str, material: str) -> str:
    return hashlib.sha256(f"{domain}:{material}".encode("utf-8")).hexdigest()


@pytest.mark.unit
def test_limiter_identifier_is_not_plain_sha256() -> None:
    settings = _settings_with_secrets()
    source_key = admin_auth.build_source_rate_limit_key(settings, "203.0.113.1")
    account_key = admin_auth.build_account_rate_limit_key(settings, "operator")
    assert source_key != _plain_sha256_identifier("src", "203.0.113.1")
    assert account_key != _plain_sha256_identifier("acct", "operator")
    assert len(source_key) == 64


@pytest.mark.unit
def test_limiter_identifier_depends_on_secret() -> None:
    settings_a = _settings_with_secrets(current=TEST_LIMITER_SECRET)
    settings_b = _settings_with_secrets(current=_OTHER_LIMITER_SECRET)
    material = "203.0.113.9"
    key_a = admin_auth.build_source_rate_limit_key(settings_a, material)
    key_b = admin_auth.build_source_rate_limit_key(settings_b, material)
    assert key_a != key_b


@pytest.mark.unit
def test_limiter_identifier_is_stable_for_same_inputs() -> None:
    settings = _settings_with_secrets()
    first = admin_auth.build_source_rate_limit_key(settings, "203.0.113.42")
    second = admin_auth.build_source_rate_limit_key(settings, "203.0.113.42")
    assert first == second


@pytest.mark.unit
def test_limiter_identifier_domain_separation() -> None:
    settings = _settings_with_secrets()
    shared_material = "203.0.113.1"
    source_key = admin_auth.build_source_rate_limit_key(settings, shared_material)
    account_key = admin_auth.build_account_rate_limit_key(settings, shared_material)
    assert source_key != account_key


@pytest.mark.unit
@pytest.mark.parametrize(
    ("secret", "field_name"),
    [
        ("", "ADMIN_LOGIN_LIMITER_SECRET"),
        ("short-secret", "ADMIN_LOGIN_LIMITER_SECRET"),
        ("placeholder", "ADMIN_LOGIN_LIMITER_SECRET"),
        ("", "ADMIN_LOGIN_LIMITER_PREVIOUS_SECRET"),
        ("short-secret", "ADMIN_LOGIN_LIMITER_PREVIOUS_SECRET"),
    ],
)
def test_limiter_secret_validation_rejects_weak_material(
    secret: str,
    field_name: str,
) -> None:
    with pytest.raises(ValueError, match=field_name):
        admin_auth.validate_admin_login_limiter_secret(secret, field_name=field_name)


@pytest.mark.unit
def test_startup_validates_limiter_secret_when_admin_auth_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", "short")
    with pytest.raises(ValueError, match="ADMIN_LOGIN_LIMITER_SECRET"):
        admin_auth.validate_admin_login_limiter_settings(get_settings())


@pytest.mark.unit
def test_limiter_settings_validation_rejects_matching_previous_secret() -> None:
    settings = _settings_with_secrets(
        current=TEST_LIMITER_SECRET,
        previous=TEST_LIMITER_SECRET,
    )
    with pytest.raises(ValueError, match="must differ"):
        admin_auth.validate_admin_login_limiter_settings(settings)


@pytest.mark.unit
def test_rotation_honors_previous_key_lockout_without_incrementing_retired_rows(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", _OTHER_LIMITER_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_PREVIOUS_SECRET", _PREVIOUS_LIMITER_SECRET)
    settings = get_settings()
    previous_source_key = admin_auth._digest_limiter_key_with_secret(
        _PREVIOUS_LIMITER_SECRET,
        "src",
        "testclient",
    )
    now = datetime.now(timezone.utc)
    rate_limit_store.rows[previous_source_key] = {
        "failure_count": 5,
        "window_started_at": now,
        "locked_until": now + timedelta(seconds=900),
        "updated_at": now,
    }

    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            response = _login(password="wrong")

    assert response.status_code == 429
    current_source_key = admin_auth.build_source_rate_limit_key(settings, "testclient")
    assert rate_limit_store.rows.get(current_source_key, {}).get("failure_count", 0) == 0


@pytest.mark.unit
def test_rotation_cleanup_removes_stale_previous_key_rows(
    rate_limit_store: FakeRateLimitStore,
) -> None:
    settings = _settings_with_secrets(
        current=_OTHER_LIMITER_SECRET,
        previous=_PREVIOUS_LIMITER_SECRET,
    )
    stale_key = admin_auth._digest_limiter_key_with_secret(
        _PREVIOUS_LIMITER_SECRET,
        "src",
        "203.0.113.88",
    )
    now = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    rate_limit_store.rows[stale_key] = {
        "failure_count": 1,
        "window_started_at": now - timedelta(hours=2),
        "locked_until": None,
        "updated_at": now - timedelta(hours=2),
    }
    deleted = rate_limit_store.cleanup(
        now,
        window_seconds=60,
        lockout_seconds=60,
    )
    assert deleted >= 1
    assert stale_key not in rate_limit_store.rows


@pytest.mark.unit
@pytest.mark.integration
def test_unknown_username_failure_records_anonymous_actor(
    rate_limit_store: FakeRateLimitStore,
) -> None:
    captured: list[dict[str, object]] = []

    def capture_append(_conn, **kwargs):
        captured.append(kwargs)
        return {"id": "evt-1"}

    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            with patch(
                "app.admin_routes.audit_service.record_login_failure",
                side_effect=lambda conn, **kwargs: (
                    captured.append(kwargs) or {"id": "evt-1"}
                ),
            ):
                with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                    response = _login(username="ghost-user", password="wrong-password")

    assert response.status_code == 401
    assert len(captured) == 1
    actor_context = captured[0]["actor_context"]
    assert actor_context.actor == "anonymous"
    assert "ghost-user" not in str(captured[0])


@pytest.mark.unit
@pytest.mark.integration
def test_configured_username_wrong_password_keeps_anonymous_actor(
    rate_limit_store: FakeRateLimitStore,
) -> None:
    captured: list[dict[str, object]] = []

    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            with patch(
                "app.admin_routes.audit_service.record_login_failure",
                side_effect=lambda conn, **kwargs: (
                    captured.append(kwargs) or {"id": "evt-1"}
                ),
            ):
                with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                    response = _login(username=TEST_USERNAME, password="wrong-password")

    assert response.status_code == 401
    assert captured[0]["actor_context"].actor == "anonymous"
    assert TEST_USERNAME not in str(captured[0])


@pytest.mark.unit
@pytest.mark.integration
def test_invalid_csrf_failure_records_anonymous_actor(
    rate_limit_store: FakeRateLimitStore,
) -> None:
    captured: list[dict[str, object]] = []

    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            with patch(
                "app.admin_routes.audit_service.record_login_failure",
                side_effect=lambda conn, **kwargs: (
                    captured.append(kwargs) or {"id": "evt-1"}
                ),
            ):
                csrf_token, cookies = _fetch_login_form()
                response = client.post(
                    "/admin/login",
                    data={
                        "username": "candidate-user",
                        "password": "wrong-password",
                        "csrf_token": "tampered-token",
                    },
                    cookies=cookies,
                )

    assert response.status_code == 400
    assert captured[0]["actor_context"].actor == "anonymous"
    assert "candidate-user" not in str(captured[0])


@pytest.mark.unit
@pytest.mark.integration
def test_successful_login_retains_authenticated_actor(
    rate_limit_store: FakeRateLimitStore,
) -> None:
    captured: list[dict[str, object]] = []

    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                with patch("app.admin_routes.db.create_admin_session", return_value=42):
                    with patch(
                        "app.admin_routes.audit_service.record_login_success",
                        side_effect=lambda conn, **kwargs: (
                            captured.append(kwargs) or {"id": "evt-success"}
                        ),
                    ):
                        response = _login(username=TEST_USERNAME, password=TEST_PASSWORD)

    assert response.status_code == 303
    assert captured[0]["actor_context"].actor == TEST_USERNAME


@pytest.mark.unit
def test_login_failure_logs_exclude_candidate_and_secret_material(
    rate_limit_store: FakeRateLimitStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    candidate = "attacker-controlled-name"
    isolated_client = TestClient(app, follow_redirects=False)
    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            with patch(
                "app.admin_routes.audit_service.record_login_failure",
                side_effect=RuntimeError("audit down"),
            ):
                with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                    form = isolated_client.get("/admin/login")
                    csrf_token, cookies = _parse_login_form(form)
                    isolated_client.post(
                        "/admin/login",
                        data={
                            "username": candidate,
                            "password": "wrong-password",
                            "csrf_token": csrf_token,
                        },
                        cookies=cookies,
                    )

    combined = caplog.text
    assert candidate not in combined
    assert TEST_LIMITER_SECRET not in combined
    assert "src:" not in combined
    assert "acct:" not in combined


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
def test_postgres_persists_keyed_identifiers_and_anonymous_failure_actor(
    pg_conn: psycopg.Connection,
) -> None:
    settings = _settings_with_secrets()
    source_key = admin_auth.build_source_rate_limit_key(settings, "203.0.113.200")
    now = datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)
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
    assert row["limiter_key"] != _plain_sha256_identifier("src", "203.0.113.200")
    assert "203.0.113.200" not in row["limiter_key"]

    repo = MagicMock()
    repo.append.return_value = {"id": "evt-pg"}
    request = MagicMock()
    request.headers = {}
    request.state = MagicMock()
    request.state.correlation_id = "corr-pg"
    actor_context = anonymous_actor_context(request)
    audit_service.record_login_failure(
        pg_conn,
        actor_context=actor_context,
        reason="invalid_credentials",
        repository=repo,
    )
    append_kwargs = repo.append.call_args.kwargs
    assert append_kwargs["actor"] == "anonymous"
    assert TEST_USERNAME not in str(append_kwargs)


@pytest.mark.integration
def test_postgres_concurrent_admission_with_hmac_keys(pg_conn: psycopg.Connection) -> None:
    settings = _settings_with_secrets()
    source_key = admin_auth.build_source_rate_limit_key(settings, "198.51.100.77")
    now = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
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
