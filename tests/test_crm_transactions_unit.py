"""Unit tests for CRM transaction ownership (repositories vs service)."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from app import audit_service, db
from app.actor_context import ActorContext
from app.crm_service import CrmRepositories, CrmService
from app.crm_uow import crm_transaction
from app.repositories.postgres import (
    PostgresActivityRepository,
    PostgresAdminUserRepository,
    PostgresAuditEventRepository,
    PostgresCompanyRepository,
    PostgresContactRepository,
    PostgresSourceRecordRepository,
)

COMPANY_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
CONTACT_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


def _mock_conn(row: dict | list | None = None) -> MagicMock:
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    if isinstance(row, list):
        cur.fetchall.return_value = row
    elif row is not None:
        cur.fetchone.return_value = row
    return conn


@pytest.mark.unit
@pytest.mark.parametrize(
    "repo_factory,create_kwargs",
    [
        (
            PostgresCompanyRepository,
            {"name": "Acme", "website": "https://acme.dev"},
        ),
        (
            PostgresContactRepository,
            {"email": "lead@example.com", "company_id": COMPANY_ID},
        ),
        (
            PostgresSourceRecordRepository,
            {
                "source_type": "manual",
                "external_id": "ext-1",
                "company_id": COMPANY_ID,
            },
        ),
        (
            PostgresActivityRepository,
            {
                "activity_type": "note",
                "summary": "Hello",
                "company_id": COMPANY_ID,
            },
        ),
        (
            PostgresAdminUserRepository,
            {"email": "admin@saberistic.com", "role": "admin"},
        ),
    ],
)
def test_repository_writes_do_not_commit(
    repo_factory: type,
    create_kwargs: dict,
) -> None:
    row = {"id": COMPANY_ID, **create_kwargs}
    conn = _mock_conn(row)
    repo = repo_factory()
    repo.create(conn, **create_kwargs)
    conn.commit.assert_not_called()
    conn.rollback.assert_not_called()


@pytest.mark.unit
def test_crm_transaction_commits_on_success() -> None:
    conn = MagicMock()
    with crm_transaction(conn):
        pass
    conn.commit.assert_called_once()
    conn.rollback.assert_not_called()


@pytest.mark.unit
def test_crm_transaction_rolls_back_on_failure() -> None:
    conn = MagicMock()
    with pytest.raises(RuntimeError, match="boom"):
        with crm_transaction(conn):
            raise RuntimeError("boom")
    conn.rollback.assert_called_once()
    conn.commit.assert_not_called()


@pytest.mark.unit
def test_crm_transaction_retry_after_rollback() -> None:
    conn = MagicMock()
    with pytest.raises(ValueError):
        with crm_transaction(conn):
            raise ValueError("first attempt")
    conn.rollback.assert_called_once()

    with crm_transaction(conn):
        pass
    conn.commit.assert_called_once()


@pytest.mark.unit
def test_record_company_with_contact_commits_once_on_success() -> None:
    company_repo = MagicMock()
    contact_repo = MagicMock()
    company_repo.create.return_value = {"id": COMPANY_ID, "name": "Acme"}
    contact_repo.create.return_value = {"id": CONTACT_ID, "email": "lead@example.com"}

    service = CrmService(
        repos=CrmRepositories(
            companies=company_repo,
            contacts=contact_repo,
            source_records=MagicMock(),
            activities=MagicMock(),
            stage_history=MagicMock(),
            research_records=MagicMock(),
            admin_users=MagicMock(),
        )
    )
    conn = MagicMock()

    bundle = service.record_company_with_contact(
        conn,
        company_name="Acme",
        website="https://acme.dev",
        contact_email="lead@example.com",
    )

    assert bundle["company"]["name"] == "Acme"
    conn.commit.assert_called_once()
    conn.rollback.assert_not_called()


@pytest.mark.unit
def test_record_company_with_contact_rolls_back_when_contact_fails() -> None:
    company_repo = MagicMock()
    contact_repo = MagicMock()
    company_repo.create.return_value = {"id": COMPANY_ID, "name": "Acme"}
    contact_repo.create.side_effect = RuntimeError("duplicate email")

    service = CrmService(
        repos=CrmRepositories(
            companies=company_repo,
            contacts=contact_repo,
            source_records=MagicMock(),
            activities=MagicMock(),
            stage_history=MagicMock(),
            research_records=MagicMock(),
            admin_users=MagicMock(),
        )
    )
    conn = MagicMock()

    with pytest.raises(RuntimeError, match="duplicate email"):
        service.record_company_with_contact(
            conn,
            company_name="Acme",
            website="https://acme.dev",
            contact_email="lead@example.com",
        )

    company_repo.create.assert_called_once()
    contact_repo.create.assert_called_once()
    conn.rollback.assert_called_once()
    conn.commit.assert_not_called()


@pytest.mark.unit
def test_single_record_service_operations_commit_once() -> None:
    activity_repo = MagicMock()
    source_repo = MagicMock()
    activity_repo.create.return_value = {"summary": "Follow up"}
    source_repo.create.return_value = {"external_id": "7"}

    service = CrmService(
        repos=CrmRepositories(
            companies=MagicMock(),
            contacts=MagicMock(),
            source_records=source_repo,
            activities=activity_repo,
            stage_history=MagicMock(),
            research_records=MagicMock(),
            admin_users=MagicMock(),
        )
    )
    conn = MagicMock()

    service.record_activity_for_company(
        conn,
        company_id=COMPANY_ID,
        activity_type="note",
        summary="Follow up",
    )
    service.link_project_brief_source(
        conn,
        brief_id=7,
        company_id=COMPANY_ID,
        contact_id=CONTACT_ID,
    )

    assert conn.commit.call_count == 2
    conn.rollback.assert_not_called()


@pytest.mark.unit
def test_read_methods_do_not_change_transaction_state() -> None:
    admin_repo = MagicMock()
    admin_repo.get_by_email.return_value = {"email": "admin@saberistic.com"}
    service = CrmService(
        repos=CrmRepositories(
            companies=MagicMock(),
            contacts=MagicMock(),
            source_records=MagicMock(),
            activities=MagicMock(),
            stage_history=MagicMock(),
            research_records=MagicMock(),
            admin_users=admin_repo,
        )
    )
    conn = MagicMock()

    user = service.get_admin_user_by_email(conn, "admin@saberistic.com")

    assert user is not None
    conn.commit.assert_not_called()
    conn.rollback.assert_not_called()


def _actor() -> ActorContext:
    return ActorContext(actor="operator", correlation_id="corr-tx")


@pytest.mark.unit
def test_audit_repository_append_does_not_commit() -> None:
    conn = _mock_conn({"id": "evt-1", "action": "import.batch"})
    repo = PostgresAuditEventRepository()
    repo.append(
        conn,
        actor="operator",
        action="import.batch",
        correlation_id="corr-1",
    )
    conn.commit.assert_not_called()
    conn.rollback.assert_not_called()


@pytest.mark.unit
def test_import_batch_commits_once_with_audit() -> None:
    source_repo = MagicMock()
    source_repo.create.return_value = {"id": "sr-1"}
    audit_repo = MagicMock()
    audit_repo.append.return_value = {"id": "evt-1"}
    service = CrmService(
        repos=CrmRepositories(
            companies=MagicMock(),
            contacts=MagicMock(),
            source_records=source_repo,
            activities=MagicMock(),
            stage_history=MagicMock(),
            research_records=MagicMock(),
            admin_users=MagicMock(),
        )
    )
    conn = MagicMock()

    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(
            "app.crm_service.audit_service.get_repositories",
            lambda: MagicMock(audit_events=audit_repo),
        )
        result = service.import_batch(
            conn,
            actor_context=_actor(),
            batch_id="batch-1",
            source_type="csv",
            records=[{"name": "Acme"}],
        )

    assert result["record_count"] == 1
    conn.commit.assert_called_once()
    conn.rollback.assert_not_called()


@pytest.mark.unit
def test_import_batch_rolls_back_business_writes_when_audit_fails() -> None:
    source_repo = MagicMock()
    source_repo.create.return_value = {"id": "sr-1"}
    audit_repo = MagicMock()
    audit_repo.append.side_effect = RuntimeError("audit insert failed")
    service = CrmService(
        repos=CrmRepositories(
            companies=MagicMock(),
            contacts=MagicMock(),
            source_records=source_repo,
            activities=MagicMock(),
            stage_history=MagicMock(),
            research_records=MagicMock(),
            admin_users=MagicMock(),
        )
    )
    conn = MagicMock()

    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(
            "app.crm_service.audit_service.get_repositories",
            lambda: MagicMock(audit_events=audit_repo),
        )
        with pytest.raises(RuntimeError, match="audit insert failed"):
            service.import_batch(
                conn,
                actor_context=_actor(),
                batch_id="batch-1",
                source_type="csv",
                records=[{"name": "Acme"}],
            )

    source_repo.create.assert_called_once()
    conn.rollback.assert_called_once()
    conn.commit.assert_not_called()


@pytest.mark.unit
def test_import_batch_rolls_back_when_business_write_fails() -> None:
    source_repo = MagicMock()
    source_repo.create.side_effect = RuntimeError("duplicate key")
    audit_repo = MagicMock()
    service = CrmService(
        repos=CrmRepositories(
            companies=MagicMock(),
            contacts=MagicMock(),
            source_records=source_repo,
            activities=MagicMock(),
            stage_history=MagicMock(),
            research_records=MagicMock(),
            admin_users=MagicMock(),
        )
    )
    conn = MagicMock()

    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(
            "app.crm_service.audit_service.get_repositories",
            lambda: MagicMock(audit_events=audit_repo),
        )
        with pytest.raises(RuntimeError, match="duplicate key"):
            service.import_batch(
                conn,
                actor_context=_actor(),
                batch_id="batch-1",
                source_type="csv",
                records=[{"name": "Acme"}],
            )

    audit_repo.append.assert_not_called()
    conn.rollback.assert_called_once()
    conn.commit.assert_not_called()


@pytest.mark.unit
def test_import_batch_retry_after_rollback() -> None:
    source_repo = MagicMock()
    source_repo.create.side_effect = [RuntimeError("transient"), {"id": "sr-1"}]
    audit_repo = MagicMock()
    audit_repo.append.return_value = {"id": "evt-1"}
    service = CrmService(
        repos=CrmRepositories(
            companies=MagicMock(),
            contacts=MagicMock(),
            source_records=source_repo,
            activities=MagicMock(),
            stage_history=MagicMock(),
            research_records=MagicMock(),
            admin_users=MagicMock(),
        )
    )
    conn = MagicMock()

    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(
            "app.crm_service.audit_service.get_repositories",
            lambda: MagicMock(audit_events=audit_repo),
        )
        with pytest.raises(RuntimeError, match="transient"):
            service.import_batch(
                conn,
                actor_context=_actor(),
                batch_id="batch-1",
                source_type="csv",
                records=[{"name": "Acme"}],
            )
        service.import_batch(
            conn,
            actor_context=_actor(),
            batch_id="batch-2",
            source_type="csv",
            records=[{"name": "Beta"}],
        )

    conn.rollback.assert_called_once()
    conn.commit.assert_called_once()


@pytest.mark.unit
def test_delete_entity_rolls_back_when_audit_fails() -> None:
    audit_repo = MagicMock()
    audit_repo.append.side_effect = RuntimeError("audit down")
    service = CrmService(
        repos=CrmRepositories(
            companies=MagicMock(),
            contacts=MagicMock(),
            source_records=MagicMock(),
            activities=MagicMock(),
            stage_history=MagicMock(),
            research_records=MagicMock(),
            admin_users=MagicMock(),
        )
    )
    conn = MagicMock()

    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(
            "app.crm_service.audit_service.get_repositories",
            lambda: MagicMock(audit_events=audit_repo),
        )
        with pytest.raises(RuntimeError, match="audit down"):
            service.delete_entity(
                conn,
                actor_context=_actor(),
                entity_type="company",
                entity_id="co-1",
            )

    conn.rollback.assert_called_once()
    conn.commit.assert_not_called()


@pytest.mark.unit
def test_login_session_and_audit_rollback_when_audit_fails() -> None:
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    cursor.fetchone.return_value = {"id": 9}
    audit_repo = MagicMock()
    audit_repo.append.side_effect = RuntimeError("audit insert failed")
    actor = ActorContext(actor="operator", correlation_id="corr-login")

    with pytest.raises(RuntimeError, match="audit insert failed"):
        with crm_transaction(conn):
            session_id = db.create_admin_session(
                conn,
                token_hash="hash",
                admin_username="operator",
                expires_at=datetime.now(timezone.utc),
            )
            audit_service.record_login_success(
                conn,
                actor_context=actor,
                session_id=session_id,
                repository=audit_repo,
            )

    conn.rollback.assert_called_once()
    conn.commit.assert_not_called()


@pytest.mark.unit
def test_login_failure_audit_is_best_effort() -> None:
    conn = MagicMock()
    repo = MagicMock()
    repo.append.side_effect = RuntimeError("audit down")
    result = audit_service.record_login_failure(
        conn,
        actor_context=_actor(),
        reason="invalid_credentials",
        repository=repo,
    )
    assert result is None
