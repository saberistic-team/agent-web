"""Tests for immutable audit events, redaction, and admin audit UI."""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Generator
from uuid import UUID

from unittest.mock import MagicMock, patch

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app import admin_auth, audit_service, db
from app.actor_context import ActorContext
from app.admin_auth import SESSION_COOKIE_NAME
from app.audit_service import REDACTED_VALUE
from app.config import get_settings
from app.crm_service import CrmRepositories, CrmService
from app.crm_uow import crm_transaction
from app.main import app
from app.migrations.definitions import MIGRATIONS
from app.migrations.runner import pending_migrations
from app.repositories.postgres import PostgresAuditEventRepository

client = TestClient(app, follow_redirects=False)

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "test-session-secret-32chars-minimum"


@pytest.fixture(autouse=True)
def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    admin_auth.reset_login_rate_limiter()


@contextmanager
def mock_db_connection() -> Generator[MagicMock, None, None]:
    conn = MagicMock()
    with patch("app.admin_routes.db.db_connection") as admin_conn:
        admin_conn.return_value.__enter__.return_value = conn
        admin_conn.return_value.__exit__.return_value = None
        yield conn


def _actor(correlation_id: str = "corr-test-1") -> ActorContext:
    return ActorContext(actor=TEST_USERNAME, correlation_id=correlation_id)


def _session_row(
    *,
    token_hash: str,
    expires_at: datetime | None = None,
    revoked_at: datetime | None = None,
    csrf_token_hash: str | None = None,
) -> dict[str, Any]:
    return {
        "id": 42,
        "token_hash": token_hash,
        "admin_username": TEST_USERNAME,
        "created_at": datetime.now(timezone.utc),
        "expires_at": expires_at or (datetime.now(timezone.utc) + timedelta(hours=1)),
        "revoked_at": revoked_at,
        "csrf_token_hash": csrf_token_hash,
    }


@pytest.mark.unit
def test_audit_migration_present_and_ordered() -> None:
    versions = [migration.version for migration in MIGRATIONS]
    assert "007" in versions
    audit = next(m for m in MIGRATIONS if m.version == "007")
    assert "audit_events" in audit.up_sql
    assert "prevent_audit_events_mutation" in audit.up_sql
    assert versions.index("007") > versions.index("006")


@pytest.mark.unit
def test_pending_migrations_includes_audit_after_sessions() -> None:
    pending = pending_migrations(applied_versions={"001", "002", "003", "004", "005", "006"})
    assert len(pending) == 6
    assert [m.version for m in pending] == ["007", "008", "009", "010", "011", "012"]


@pytest.mark.unit
def test_redact_summary_strips_sensitive_fields() -> None:
    raw = {
        "status": "active",
        "password": "hunter2",
        "stripe_session_id": "cs_live_secret",
        "email": "ops@example.com",
        "nested": {"api_key": "sk_test", "count": 3},
    }
    safe = audit_service.redact_summary(raw)
    assert safe is not None
    assert safe["status"] == "active"
    assert safe["password"] == REDACTED_VALUE
    assert safe["stripe_session_id"] == REDACTED_VALUE
    assert safe["email"] == REDACTED_VALUE
    assert safe["nested"]["api_key"] == REDACTED_VALUE
    assert safe["nested"]["count"] == 3


@pytest.mark.unit
def test_record_event_redacts_before_persisting() -> None:
    conn = MagicMock()
    repo = MagicMock()
    repo.append.return_value = {"id": "evt-1"}
    actor = _actor()
    audit_service.record_event(
        conn,
        actor_context=actor,
        action=audit_service.ACTION_EXPORT_REQUEST,
        entity_type="export",
        entity_id="companies_csv",
        summary_after={"export_type": "companies_csv", "password": "secret"},
        repository=repo,
    )
    kwargs = repo.append.call_args.kwargs
    assert kwargs["summary_after"]["password"] == REDACTED_VALUE


@pytest.mark.unit
def test_representative_mutation_helpers_call_record_event() -> None:
    conn = MagicMock()
    repo = MagicMock()
    repo.append.return_value = {"id": "evt-2"}
    actor = _actor("corr-mutations")

    audit_service.record_import_batch(
        conn,
        actor_context=actor,
        batch_id="batch-1",
        source_type="linkedin",
        record_count=12,
        repository=repo,
    )
    audit_service.record_entity_delete(
        conn,
        actor_context=actor,
        entity_type="company",
        entity_id="co-9",
        summary_before={"name": "Acme", "email": "hidden@example.com"},
        repository=repo,
    )
    audit_service.record_pipeline_update(
        conn,
        actor_context=actor,
        entity_id="deal-1",
        summary_before={"stage": "qualified"},
        summary_after={"stage": "proposal"},
        repository=repo,
    )
    audit_service.record_scoring_rule_update(
        conn,
        actor_context=actor,
        rule_id="icp-weight-revenue",
        summary_before={"weight": 10},
        summary_after={"weight": 20},
        repository=repo,
    )
    audit_service.record_analytics_config_update(
        conn,
        actor_context=actor,
        config_key="funnel",
        summary_before={"enabled": False},
        summary_after={"enabled": True},
        repository=repo,
    )
    audit_service.record_export_request(
        conn,
        actor_context=actor,
        export_type="pipeline_csv",
        filters={"stage": "qualified"},
        repository=repo,
    )

    actions = [call.kwargs["action"] for call in repo.append.call_args_list]
    assert actions == [
        audit_service.ACTION_IMPORT_BATCH,
        audit_service.ACTION_ENTITY_DELETE,
        audit_service.ACTION_PIPELINE_UPDATE,
        audit_service.ACTION_SCORING_RULE_UPDATE,
        audit_service.ACTION_ANALYTICS_CONFIG_UPDATE,
        audit_service.ACTION_EXPORT_REQUEST,
    ]
    delete_call = repo.append.call_args_list[1].kwargs
    assert delete_call["summary_before"]["email"] == REDACTED_VALUE


@pytest.mark.unit
def test_crm_service_audited_mutations_record_events() -> None:
    conn = MagicMock()
    source_repo = MagicMock()
    source_repo.create.return_value = {"id": "sr-1"}
    audit_repo = MagicMock()
    actor = _actor("corr-crm")

    service = CrmService(
        repos=CrmRepositories(
            companies=MagicMock(),
            contacts=MagicMock(),
            source_records=source_repo,
            activities=MagicMock(),
            research_records=MagicMock(),
            admin_users=MagicMock(),
        )
    )

    with patch("app.crm_service.audit_service.record_import_batch", wraps=audit_service.record_import_batch) as import_audit:
        with patch("app.crm_service.audit_service.record_entity_delete", wraps=audit_service.record_entity_delete) as delete_audit:
            with patch("app.crm_service.audit_service.record_pipeline_update", wraps=audit_service.record_pipeline_update) as pipeline_audit:
                with patch("app.crm_service.audit_service.record_scoring_rule_update", wraps=audit_service.record_scoring_rule_update) as scoring_audit:
                    with patch("app.crm_service.audit_service.record_analytics_config_update", wraps=audit_service.record_analytics_config_update) as analytics_audit:
                        with patch("app.crm_service.audit_service.record_export_request", wraps=audit_service.record_export_request) as export_audit:
                            with patch("app.crm_service.audit_service.get_repositories") as get_repos:
                                get_repos.return_value.audit_events = audit_repo
                                audit_repo.append.return_value = {"id": "evt"}

                                service.import_batch(
                                    conn,
                                    actor_context=actor,
                                    batch_id="batch-9",
                                    source_type="csv",
                                    records=[{"name": "Acme"}],
                                )
                                service.delete_entity(
                                    conn,
                                    actor_context=actor,
                                    entity_type="company",
                                    entity_id="co-1",
                                    summary_before={"name": "Acme"},
                                )
                                service.update_pipeline(
                                    conn,
                                    actor_context=actor,
                                    entity_id="deal-1",
                                    summary_before={"stage": "new"},
                                    summary_after={"stage": "qualified"},
                                )
                                service.update_scoring_rule(
                                    conn,
                                    actor_context=actor,
                                    rule_id="rule-1",
                                    summary_before={"weight": 1},
                                    summary_after={"weight": 2},
                                )
                                service.update_analytics_config(
                                    conn,
                                    actor_context=actor,
                                    config_key="funnel",
                                    summary_before={"enabled": False},
                                    summary_after={"enabled": True},
                                )
                                service.request_export(
                                    conn,
                                    actor_context=actor,
                                    export_type="companies_csv",
                                    filters={"status": "active"},
                                )

    import_audit.assert_called_once()
    delete_audit.assert_called_once()
    pipeline_audit.assert_called_once()
    scoring_audit.assert_called_once()
    analytics_audit.assert_called_once()
    export_audit.assert_called_once()
    assert source_repo.create.call_count == 1
    assert conn.commit.call_count == 6
    conn.rollback.assert_not_called()


@pytest.mark.unit
def test_postgres_audit_repository_append_serializes_json() -> None:
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    cursor.fetchone.return_value = {
        "id": UUID("00000000-0000-0000-0000-000000000101"),
        "action": "entity.delete",
    }
    repo = PostgresAuditEventRepository()
    repo.append(
        conn,
        actor=TEST_USERNAME,
        action="entity.delete",
        correlation_id="corr-99",
        summary_after={"status": "deleted"},
    )
    sql_args = cursor.execute.call_args[0]
    assert "INSERT INTO audit_events" in sql_args[0]
    assert json.loads(sql_args[1][6]) == {"status": "deleted"}
    conn.commit.assert_not_called()
    conn.rollback.assert_not_called()


@pytest.mark.unit
def test_audit_repository_has_no_update_or_delete_methods() -> None:
    repo = PostgresAuditEventRepository()
    assert not hasattr(repo, "update")
    assert not hasattr(repo, "delete")
    assert hasattr(repo, "append")
    assert hasattr(repo, "list_page")


@pytest.mark.unit
def test_authenticated_logout_audit_is_required() -> None:
    conn = MagicMock()
    repo = MagicMock()
    repo.append.side_effect = RuntimeError("audit down")
    with pytest.raises(RuntimeError, match="audit down"):
        audit_service.record_logout(
            conn,
            actor_context=_actor(),
            session_id=42,
            repository=repo,
        )


@pytest.mark.unit
def test_anonymous_logout_audit_is_best_effort() -> None:
    conn = MagicMock()
    repo = MagicMock()
    repo.append.side_effect = RuntimeError("audit down")
    result = audit_service.record_logout(
        conn,
        actor_context=_actor(),
        session_id=None,
        repository=repo,
    )
    assert result is None


@pytest.mark.unit
@pytest.mark.integration
def test_login_success_uses_single_transaction_for_session_and_audit() -> None:
    with mock_db_connection() as conn:
        with patch("app.admin_routes._verify_login_flow_csrf", return_value=True):
            with patch("app.admin_routes._consume_login_flow"):
                with patch("app.admin_routes.crm_transaction", wraps=crm_transaction) as tx:
                    with patch(
                        "app.admin_routes.db.create_admin_session", return_value=42
                    ) as create_session:
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
                            tx.assert_called_once()
                            create_session.assert_called_once()
                            success_audit.assert_called_once()


@pytest.mark.unit
@pytest.mark.integration
def test_login_success_and_failure_create_audit_events() -> None:
    with mock_db_connection() as conn:
        with patch("app.admin_routes._verify_login_flow_csrf", return_value=True):
            with patch("app.admin_routes._consume_login_flow"):
                with patch(
                    "app.admin_routes.db.create_admin_session", return_value=42
                ) as create_session:
                    with patch(
                        "app.admin_routes.audit_service.record_login_success"
                    ) as success_audit:
                        with patch(
                            "app.admin_routes.audit_service.record_login_failure"
                        ) as failure_audit:
                            login = client.post(
                                "/admin/login",
                                data={
                                    "username": TEST_USERNAME,
                                    "password": TEST_PASSWORD,
                                    "csrf_token": "flow-csrf",
                                },
                            )
                            assert login.status_code == 303
                            create_session.assert_called_once()
                            success_audit.assert_called_once()
                            assert success_audit.call_args.kwargs["session_id"] == 42

                            bad_login = client.post(
                                "/admin/login",
                                data={
                                    "username": TEST_USERNAME,
                                    "password": "wrong-password",
                                    "csrf_token": "flow-csrf",
                                },
                            )
                            assert bad_login.status_code == 401
                            failure_audit.assert_called_once()
                            assert (
                                failure_audit.call_args.kwargs["reason"]
                                == "invalid_credentials"
                            )


@pytest.mark.unit
@pytest.mark.integration
def test_logout_records_audit_event() -> None:
    raw_session_token = "session-token"
    raw_csrf = admin_auth.derive_session_csrf_token(raw_session_token, get_settings())
    token_hash = admin_auth.hash_session_token(raw_session_token)
    row = _session_row(
        token_hash=token_hash,
        csrf_token_hash=admin_auth.hash_csrf_token(raw_csrf),
    )
    with mock_db_connection() as conn:
        with patch("app.admin_routes.db.get_admin_session_by_token_hash", return_value=row):
            with patch("app.admin_routes.db.revoke_admin_session") as revoke_session:
                with patch("app.admin_routes.audit_service.record_logout") as logout_audit:
                    response = client.post(
                        "/admin/logout",
                        data={"csrf_token": raw_csrf},
                        cookies={SESSION_COOKIE_NAME: raw_session_token},
                    )
                    assert response.status_code == 303
                    revoke_session.assert_called_once()
                    logout_audit.assert_called_once()
                    assert logout_audit.call_args.kwargs["session_id"] == 42


@pytest.mark.unit
@pytest.mark.integration
def test_admin_audit_page_requires_auth() -> None:
    response = client.get("/admin/audit")
    assert response.status_code == 303
    assert "/admin/login" in response.headers["location"]


@pytest.mark.unit
@pytest.mark.integration
def test_admin_audit_page_renders_paginated_events() -> None:
    token_hash = admin_auth.hash_session_token("audit-session")
    row = _session_row(token_hash=token_hash)
    events = [
        {
            "id": UUID("00000000-0000-0000-0000-000000000001"),
            "created_at": datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc),
            "actor": TEST_USERNAME,
            "action": audit_service.ACTION_AUTH_LOGIN_SUCCESS,
            "entity_type": "admin_session",
            "entity_id": "42",
            "correlation_id": "corr-abc",
            "summary_before": None,
            "summary_after": {"session_id": 42},
            "metadata": None,
        }
    ]
    with mock_db_connection() as conn:
        with patch("app.admin_routes.db.get_admin_session_by_token_hash", return_value=row):
            with patch(
                "app.admin_routes.audit_service.list_events",
                return_value=(events, 1),
            ) as list_events:
                response = client.get(
                    "/admin/audit?page=1",
                    cookies={SESSION_COOKIE_NAME: "audit-session"},
                )
                assert response.status_code == 200
                assert "Immutable audit log" in response.text
                assert "auth.login.success" in response.text
                assert "corr-abc" in response.text
                list_events.assert_called_once()


@pytest.mark.unit
def test_correlation_id_header_is_echoed() -> None:
    response = client.get("/health", headers={"X-Request-ID": "trace-123"})
    assert response.status_code == 200
    assert response.headers.get("x-request-id") == "trace-123"


@pytest.mark.unit
def test_actor_context_prefers_request_header() -> None:
    from app.actor_context import actor_context_from_request, correlation_id_from_request

    request = MagicMock()
    request.headers = {"x-request-id": " inbound-trace "}
    request.state = MagicMock()
    request.state.correlation_id = "state-id"
    assert correlation_id_from_request(request) == "inbound-trace"
    ctx = actor_context_from_request(request, actor=TEST_USERNAME)
    assert ctx.actor == TEST_USERNAME
    assert ctx.correlation_id == "inbound-trace"


@pytest.mark.unit
def test_audit_login_and_logout_helpers() -> None:
    conn = MagicMock()
    repo = MagicMock()
    repo.append.return_value = {"id": "evt-auth"}
    actor = _actor("corr-auth")

    audit_service.record_login_success(conn, actor_context=actor, session_id=9, repository=repo)
    audit_service.record_login_failure(
        conn,
        actor_context=actor,
        reason="invalid_credentials",
        attempted_username="ghost",
        repository=repo,
    )
    audit_service.record_logout(conn, actor_context=actor, session_id=9, repository=repo)

    actions = [call.kwargs["action"] for call in repo.append.call_args_list]
    assert actions == [
        audit_service.ACTION_AUTH_LOGIN_SUCCESS,
        audit_service.ACTION_AUTH_LOGIN_FAILURE,
        audit_service.ACTION_AUTH_LOGOUT,
    ]


@pytest.mark.unit
def test_record_event_raises_when_required_and_repository_fails() -> None:
    conn = MagicMock()
    repo = MagicMock()
    repo.append.side_effect = RuntimeError("db down")
    with pytest.raises(RuntimeError, match="db down"):
        audit_service.record_event(
            conn,
            actor_context=_actor(),
            action="entity.delete",
            repository=repo,
            required=True,
        )


@pytest.mark.unit
def test_record_event_swallows_repository_errors_when_not_required() -> None:
    conn = MagicMock()
    repo = MagicMock()
    repo.append.side_effect = RuntimeError("db down")
    result = audit_service.record_event(
        conn,
        actor_context=_actor(),
        action="entity.delete",
        repository=repo,
        required=False,
    )
    assert result is None


@pytest.mark.unit
def test_postgres_audit_repository_list_page() -> None:
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    cursor.fetchone.return_value = {"total": 3}
    cursor.fetchall.return_value = [
        {"id": "1", "action": "auth.login.success"},
        {"id": "2", "action": "auth.logout"},
    ]
    repo = PostgresAuditEventRepository()
    rows, total = repo.list_page(conn, page=2, per_page=2)
    assert total == 3
    assert len(rows) == 2
    list_sql = cursor.execute.call_args_list[1][0][0]
    assert "LIMIT" in list_sql


@pytest.mark.unit
def test_db_session_helpers() -> None:
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    cursor.fetchone.side_effect = [
        {"id": 7},
        {"id": 7, "admin_username": TEST_USERNAME, "revoked_at": None},
    ]

    session_id = db.create_admin_session(
        conn,
        token_hash="hash",
        admin_username=TEST_USERNAME,
        expires_at=datetime.now(timezone.utc),
    )
    assert session_id == 7

    row = db.get_admin_session_by_token_hash(conn, "hash")
    assert row["id"] == 7

    db.revoke_admin_session(conn, token_hash="hash")
    conn.commit.assert_not_called()
    conn.rollback.assert_not_called()


@pytest.mark.unit
def test_render_admin_audit_page_empty_state() -> None:
    from app.admin_pages import render_admin_audit_page

    html_out = render_admin_audit_page(
        admin_username=TEST_USERNAME,
        events=[],
        page=1,
        per_page=50,
        total=0,
    )
    assert "No audit events recorded yet." in html_out
    assert "Audit log" in html_out


@pytest.mark.unit
def test_render_admin_section_empty_state() -> None:
    from app import admin

    html_out = admin.render_admin_page(
        "/admin/companies",
        admin_username=TEST_USERNAME,
    )
    assert "Companies" in html_out
    assert "CRM data model" in html_out


@pytest.mark.unit
def test_render_admin_page_preview_dashboard(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import admin

    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "42")
    html_out = admin.render_admin_page("/admin", admin_username=TEST_USERNAME)
    assert "Today&apos;s attention" in html_out
    assert "Preview data — not production" in html_out


@pytest.mark.unit
def test_redact_value_truncates_long_strings() -> None:
    long_text = "x" * 600
    safe = audit_service.redact_value(long_text)
    assert isinstance(safe, str)
    assert len(safe) < len(long_text)
    assert safe.endswith("…")


@pytest.mark.unit
def test_list_events_clamps_page_size() -> None:
    conn = MagicMock()
    repo = MagicMock()
    repo.list_page.return_value = ([], 0)
    audit_service.list_events(conn, page=0, per_page=500, repository=repo)
    repo.list_page.assert_called_once_with(conn, page=1, per_page=100)


@pytest.mark.unit
def test_get_repositories_returns_singleton() -> None:
    from app.repositories.postgres import get_repositories

    assert get_repositories() is get_repositories()


@pytest.mark.unit
def test_audit_migration_triggers_reject_mutations_at_runtime() -> None:
    """Verify append-only enforcement is defined in migration SQL."""
    audit = next(m for m in MIGRATIONS if m.name == "audit_events")
    sql = audit.up_sql
    assert "BEFORE UPDATE ON audit_events" in sql
    assert "BEFORE DELETE ON audit_events" in sql
    assert "RAISE EXCEPTION 'audit_events records are append-only'" in sql
