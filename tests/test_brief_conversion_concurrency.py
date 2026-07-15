"""Concurrent brief conversion tests against a transactional database fixture."""

from __future__ import annotations

import threading
from copy import deepcopy
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import errors as pg_errors

from app.actor_context import ActorContext
from app.brief_conversion_lock import ADVISORY_XACT_LOCK_SQL
from app.crm_service import CrmRepositories, CrmService

pytestmark = [pytest.mark.integration]

ACTOR = ActorContext(actor="operator", correlation_id="corr-concurrent")
BRIEF_ID = 42


def _unique_violation(
    message: str,
    *,
    constraint_name: str,
    table_name: str,
) -> pg_errors.UniqueViolation:
    diag = MagicMock(constraint_name=constraint_name, table_name=table_name)

    class _WithDiag(pg_errors.UniqueViolation):
        @property
        def diag(self) -> MagicMock:
            return diag

    return _WithDiag(message)


def _brief(*, status: str = "paid") -> dict[str, Any]:
    return {
        "id": BRIEF_ID,
        "website": "https://acme.example",
        "contact_value": "ops@acme.example",
        "status": status,
        "utm_source": "linkedin",
    }


class _SharedBriefConversionDatabase:
    """Simulates Postgres state, advisory locks, and per-connection transactions."""

    def __init__(self) -> None:
        self._state_lock = threading.Lock()
        self._brief_locks: dict[int, threading.Lock] = {}
        self._committed: dict[str, Any] = {
            "companies": {},
            "contacts": {},
            "source_records": {},
            "activities": [],
            "stage_history": [],
            "audit_events": [],
        }
        self._connections: dict[int, dict[str, Any]] = {}

    def _brief_lock(self, brief_id: int) -> threading.Lock:
        with self._state_lock:
            if brief_id not in self._brief_locks:
                self._brief_locks[brief_id] = threading.Lock()
            return self._brief_locks[brief_id]

    def register_connection(self, conn: MagicMock) -> None:
        conn_id = id(conn)
        self._connections[conn_id] = {
            "pending": {
                "companies": {},
                "contacts": {},
                "source_records": {},
                "activities": [],
                "stage_history": [],
                "audit_events": [],
            },
            "held_brief_id": None,
        }

    def acquire_xact_lock(self, conn: MagicMock, brief_id: int) -> None:
        self._brief_lock(brief_id).acquire()
        self._connections[id(conn)]["held_brief_id"] = brief_id

    def release_xact_lock(self, conn: MagicMock) -> None:
        held = self._connections[id(conn)]["held_brief_id"]
        if held is not None:
            lock = self._brief_lock(held)
            if lock.locked():
                lock.release()
            self._connections[id(conn)]["held_brief_id"] = None

    def commit(self, conn: MagicMock) -> None:
        with self._state_lock:
            pending = self._connections[id(conn)]["pending"]
            self._committed["companies"].update(pending["companies"])
            self._committed["contacts"].update(pending["contacts"])
            self._committed["source_records"].update(pending["source_records"])
            self._committed["activities"].extend(pending["activities"])
            self._committed["stage_history"].extend(pending["stage_history"])
            self._committed["audit_events"].extend(pending["audit_events"])
            pending["companies"].clear()
            pending["contacts"].clear()
            pending["source_records"].clear()
            pending["activities"].clear()
            pending["stage_history"].clear()
            pending["audit_events"].clear()
        self.release_xact_lock(conn)

    def rollback(self, conn: MagicMock) -> None:
        pending = self._connections[id(conn)]["pending"]
        pending["companies"].clear()
        pending["contacts"].clear()
        pending["source_records"].clear()
        pending["activities"].clear()
        pending["stage_history"].clear()
        pending["audit_events"].clear()
        self.release_xact_lock(conn)

    def _view(self, conn: MagicMock) -> dict[str, Any]:
        pending = self._connections[id(conn)]["pending"]
        return {
            "companies": {**self._committed["companies"], **pending["companies"]},
            "contacts": {**self._committed["contacts"], **pending["contacts"]},
            "source_records": {
                **self._committed["source_records"],
                **pending["source_records"],
            },
            "activities": [*self._committed["activities"], *pending["activities"]],
            "stage_history": [
                *self._committed["stage_history"],
                *pending["stage_history"],
            ],
            "audit_events": [
                *self._committed["audit_events"],
                *pending["audit_events"],
            ],
        }

    def find_company_by_domain(self, conn: MagicMock, domain: str) -> list[dict[str, Any]]:
        view = self._view(conn)
        return [
            row
            for row in view["companies"].values()
            if row.get("domain") == domain
        ]

    def get_company(self, conn: MagicMock, company_id: UUID) -> dict[str, Any] | None:
        return self._view(conn)["companies"].get(company_id)

    def create_company(
        self,
        conn: MagicMock,
        *,
        name: str,
        website: str,
        domain: str | None,
    ) -> dict[str, Any]:
        company_id = uuid4()
        row = {
            "id": company_id,
            "name": name,
            "website": website,
            "domain": domain,
            "pipeline_stage": "researching",
        }
        self._connections[id(conn)]["pending"]["companies"][company_id] = row
        return row

    def get_contact_by_email(self, conn: MagicMock, email: str) -> dict[str, Any] | None:
        view = self._view(conn)
        for row in view["contacts"].values():
            if row.get("email") == email:
                return row
        return None

    def get_contact(self, conn: MagicMock, contact_id: UUID) -> dict[str, Any] | None:
        return self._view(conn)["contacts"].get(contact_id)

    def create_contact(
        self,
        conn: MagicMock,
        *,
        email: str,
        full_name: str,
        company_id: UUID,
    ) -> dict[str, Any]:
        contact_id = uuid4()
        row = {
            "id": contact_id,
            "email": email,
            "full_name": full_name,
            "company_id": company_id,
        }
        self._connections[id(conn)]["pending"]["contacts"][contact_id] = row
        return row

    def update_pipeline_fields(
        self,
        conn: MagicMock,
        company_id: UUID,
        *,
        pipeline_stage: str,
        expected_value_cents: int | None,
    ) -> dict[str, Any]:
        view = self._view(conn)
        company = view["companies"].get(company_id)
        if company is None:
            return {}
        updated = deepcopy(company)
        updated["pipeline_stage"] = pipeline_stage
        if expected_value_cents is not None:
            updated["expected_value_cents"] = expected_value_cents
        self._connections[id(conn)]["pending"]["companies"][company_id] = updated
        return updated

    def record_stage_history(
        self,
        conn: MagicMock,
        *,
        company_id: UUID,
        from_stage: str | None,
        to_stage: str,
        changed_by: str,
        metadata: dict[str, Any],
    ) -> None:
        self._connections[id(conn)]["pending"]["stage_history"].append(
            {
                "company_id": company_id,
                "from_stage": from_stage,
                "to_stage": to_stage,
                "changed_by": changed_by,
                "metadata": metadata,
            }
        )

    def get_source(
        self,
        conn: MagicMock,
        *,
        source_type: str,
        external_id: str,
    ) -> dict[str, Any] | None:
        key = (source_type, external_id)
        return self._view(conn)["source_records"].get(key)

    def create_source(
        self,
        conn: MagicMock,
        *,
        source_type: str,
        external_id: str,
        company_id: UUID,
        contact_id: UUID,
        payload: dict[str, Any],
        force_unique_violation: bool = False,
    ) -> dict[str, Any]:
        key = (source_type, external_id)
        if force_unique_violation or key in self._committed["source_records"]:
            raise _unique_violation(
                "duplicate source link",
                constraint_name="source_records_type_external_unique",
                table_name="source_records",
            )
        pending = self._connections[id(conn)]["pending"]["source_records"]
        if key in pending:
            raise _unique_violation(
                "duplicate source link",
                constraint_name="source_records_type_external_unique",
                table_name="source_records",
            )
        record_id = uuid4()
        row = {
            "id": record_id,
            "source_type": source_type,
            "external_id": external_id,
            "company_id": company_id,
            "contact_id": contact_id,
            "payload": payload,
        }
        pending[key] = row
        return row

    def create_activity(self, conn: MagicMock, **fields: Any) -> dict[str, Any]:
        row = {"id": uuid4(), **fields}
        self._connections[id(conn)]["pending"]["activities"].append(row)
        return row

    def record_audit(self, conn: MagicMock, **fields: Any) -> dict[str, Any]:
        row = {"id": uuid4(), **fields}
        self._connections[id(conn)]["pending"]["audit_events"].append(row)
        return row


def _make_conn(shared_db: _SharedBriefConversionDatabase) -> MagicMock:
    conn = MagicMock()
    shared_db.register_connection(conn)
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur

    def execute(sql: str, params: tuple[Any, ...] | None = None) -> None:
        if sql == ADVISORY_XACT_LOCK_SQL:
            assert params is not None
            shared_db.acquire_xact_lock(conn, int(params[1]))
            return

    cur.execute.side_effect = execute
    conn.commit.side_effect = lambda: shared_db.commit(conn)
    conn.rollback.side_effect = lambda: shared_db.rollback(conn)
    return conn


class _InMemoryCompanyRepo:
    def __init__(self, db: _SharedBriefConversionDatabase) -> None:
        self._db = db

    def find_by_domain(self, conn: MagicMock, domain: str) -> list[dict[str, Any]]:
        return self._db.find_company_by_domain(conn, domain)

    def get_by_id(self, conn: MagicMock, company_id: UUID) -> dict[str, Any] | None:
        return self._db.get_company(conn, company_id)

    def create(
        self,
        conn: MagicMock,
        *,
        name: str,
        website: str | None = None,
        domain: str | None = None,
    ) -> dict[str, Any]:
        return self._db.create_company(
            conn,
            name=name,
            website=website or "",
            domain=domain,
        )


class _InMemoryContactRepo:
    def __init__(self, db: _SharedBriefConversionDatabase) -> None:
        self._db = db

    def get_by_email(self, conn: MagicMock, email: str) -> dict[str, Any] | None:
        return self._db.get_contact_by_email(conn, email)

    def get_by_id(self, conn: MagicMock, contact_id: UUID) -> dict[str, Any] | None:
        return self._db.get_contact(conn, contact_id)

    def create(
        self,
        conn: MagicMock,
        *,
        email: str,
        full_name: str,
        company_id: UUID,
    ) -> dict[str, Any]:
        return self._db.create_contact(
            conn,
            email=email,
            full_name=full_name,
            company_id=company_id,
        )


class _InMemoryPipelineRepo:
    def __init__(self, db: _SharedBriefConversionDatabase) -> None:
        self._db = db

    def update_pipeline_fields(
        self,
        conn: MagicMock,
        company_id: UUID,
        *,
        pipeline_stage: str | None = None,
        expected_value_cents: int | None = None,
        **_: Any,
    ) -> dict[str, Any] | None:
        if pipeline_stage is None:
            return self._db.get_company(conn, company_id)
        return self._db.update_pipeline_fields(
            conn,
            company_id,
            pipeline_stage=pipeline_stage,
            expected_value_cents=expected_value_cents,
        )

    def record_stage_history(
        self,
        conn: MagicMock,
        *,
        company_id: UUID,
        from_stage: str | None,
        to_stage: str,
        changed_by: str,
        metadata: dict[str, Any],
    ) -> None:
        self._db.record_stage_history(
            conn,
            company_id=company_id,
            from_stage=from_stage,
            to_stage=to_stage,
            changed_by=changed_by,
            metadata=metadata,
        )


class _InMemorySourceRecordRepo:
    def __init__(self, db: _SharedBriefConversionDatabase) -> None:
        self._db = db
        self.force_unique_violation = False

    def get_by_source(
        self,
        conn: MagicMock,
        *,
        source_type: str,
        external_id: str,
    ) -> dict[str, Any] | None:
        return self._db.get_source(
            conn,
            source_type=source_type,
            external_id=external_id,
        )

    def create(
        self,
        conn: MagicMock,
        *,
        source_type: str,
        external_id: str | None = None,
        company_id: UUID | None = None,
        contact_id: UUID | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        assert external_id is not None
        assert company_id is not None
        assert contact_id is not None
        return self._db.create_source(
            conn,
            source_type=source_type,
            external_id=external_id,
            company_id=company_id,
            contact_id=contact_id,
            payload=payload or {},
            force_unique_violation=self.force_unique_violation,
        )


class _InMemoryActivityRepo:
    def __init__(self, db: _SharedBriefConversionDatabase) -> None:
        self._db = db

    def create(self, conn: MagicMock, **fields: Any) -> dict[str, Any]:
        return self._db.create_activity(conn, **fields)


def _conversion_service(shared_db: _SharedBriefConversionDatabase) -> CrmService:
    return CrmService(
        repos=CrmRepositories(
            companies=_InMemoryCompanyRepo(shared_db),
            contacts=_InMemoryContactRepo(shared_db),
            source_records=_InMemorySourceRecordRepo(shared_db),
            activities=_InMemoryActivityRepo(shared_db),
            research_records=MagicMock(),
            admin_users=MagicMock(),
            pipeline=_InMemoryPipelineRepo(shared_db),
            import_batches=MagicMock(),
        )
    )


def test_concurrent_conversions_commit_one_entity_set() -> None:
    shared_db = _SharedBriefConversionDatabase()
    service = _conversion_service(shared_db)
    results: list[dict[str, Any]] = []
    errors: list[BaseException] = []

    def run_conversion() -> None:
        conn = _make_conn(shared_db)
        try:
            with patch("app.crm_service.audit_service.record_brief_convert") as audit:
                audit.side_effect = lambda connection, **kwargs: shared_db.record_audit(
                    connection,
                    **kwargs,
                )
                result = service.convert_project_brief(
                    conn,
                    brief=_brief(),
                    actor_context=ACTOR,
                    price_cents=20_000,
                    company_choice="new",
                    contact_choice="new",
                )
            results.append(result)
        except BaseException as exc:  # pragma: no cover - surfaced via errors list
            errors.append(exc)

    threads = [threading.Thread(target=run_conversion) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(results) == 2
    assert len(shared_db._committed["source_records"]) == 1
    assert len(shared_db._committed["companies"]) == 1
    assert len(shared_db._committed["contacts"]) == 1
    assert len(shared_db._committed["activities"]) == 1
    assert len(shared_db._committed["stage_history"]) == 1
    assert len(shared_db._committed["audit_events"]) == 1

    winner = next(iter(shared_db._committed["source_records"].values()))
    for result in results:
        assert result["company"]["id"] == winner["company_id"]
        assert result["contact"]["id"] == winner["contact_id"]
        assert result["source_record"]["id"] == winner["id"]
    assert sum(1 for result in results if result["idempotent"]) == 1
    assert sum(1 for result in results if not result["idempotent"]) == 1


def test_sequential_repeated_conversion_is_idempotent() -> None:
    shared_db = _SharedBriefConversionDatabase()
    service = _conversion_service(shared_db)
    conn = _make_conn(shared_db)

    with patch("app.crm_service.audit_service.record_brief_convert") as audit:
        audit.side_effect = lambda connection, **kwargs: shared_db.record_audit(
            connection,
            **kwargs,
        )
        first = service.convert_project_brief(
            conn,
            brief=_brief(),
            actor_context=ACTOR,
            price_cents=20_000,
            company_choice="new",
            contact_choice="new",
        )
        second = service.convert_project_brief(
            conn,
            brief=_brief(),
            actor_context=ACTOR,
            price_cents=20_000,
            company_choice="new",
            contact_choice="new",
        )

    assert first["idempotent"] is False
    assert second["idempotent"] is True
    assert second["company"]["id"] == first["company"]["id"]
    assert len(shared_db._committed["source_records"]) == 1
    assert len(shared_db._committed["companies"]) == 1
    assert len(shared_db._committed["audit_events"]) == 1


def test_retry_after_failed_transaction_succeeds() -> None:
    shared_db = _SharedBriefConversionDatabase()
    service = _conversion_service(shared_db)
    conn = _make_conn(shared_db)
    attempts = {"count": 0}

    def flaky_audit(connection: MagicMock, **kwargs: Any) -> dict[str, Any]:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("audit failed")
        return shared_db.record_audit(connection, **kwargs)

    with patch(
        "app.crm_service.audit_service.record_brief_convert",
        side_effect=flaky_audit,
    ):
        with pytest.raises(RuntimeError, match="audit failed"):
            service.convert_project_brief(
                conn,
                brief=_brief(),
                actor_context=ACTOR,
                price_cents=20_000,
                company_choice="new",
                contact_choice="new",
            )
        result = service.convert_project_brief(
            conn,
            brief=_brief(),
            actor_context=ACTOR,
            price_cents=20_000,
            company_choice="new",
            contact_choice="new",
        )

    assert result["idempotent"] is False
    assert len(shared_db._committed["source_records"]) == 1
    assert len(shared_db._committed["companies"]) == 1
    assert len(shared_db._committed["activities"]) == 1
    assert len(shared_db._committed["audit_events"]) == 1


def test_unique_violation_race_returns_winner_without_partial_writes() -> None:
    shared_db = _SharedBriefConversionDatabase()
    source_repo = _InMemorySourceRecordRepo(shared_db)
    source_repo.force_unique_violation = True
    service = CrmService(
        repos=CrmRepositories(
            companies=_InMemoryCompanyRepo(shared_db),
            contacts=_InMemoryContactRepo(shared_db),
            source_records=source_repo,
            activities=_InMemoryActivityRepo(shared_db),
            research_records=MagicMock(),
            admin_users=MagicMock(),
            pipeline=_InMemoryPipelineRepo(shared_db),
            import_batches=MagicMock(),
        )
    )
    conn = _make_conn(shared_db)
    winner_source = {
        "id": uuid4(),
        "source_type": "project_brief",
        "external_id": str(BRIEF_ID),
        "company_id": uuid4(),
        "contact_id": uuid4(),
        "payload": {"pipeline_stage": "diagnostic_paid"},
    }
    shared_db._committed["source_records"][("project_brief", str(BRIEF_ID))] = winner_source
    shared_db._committed["companies"][winner_source["company_id"]] = {
        "id": winner_source["company_id"],
        "name": "Acme",
        "pipeline_stage": "diagnostic_paid",
    }
    shared_db._committed["contacts"][winner_source["contact_id"]] = {
        "id": winner_source["contact_id"],
        "email": "ops@acme.example",
        "company_id": winner_source["company_id"],
    }

    with patch(
        "app.crm_service.acquire_brief_conversion_lock",
        lambda _conn, _brief_id: None,
    ):
        with patch("app.crm_service.audit_service.record_brief_convert") as audit:
            audit.side_effect = lambda connection, **kwargs: shared_db.record_audit(
                connection,
                **kwargs,
            )
            result = service.convert_project_brief(
                conn,
                brief=_brief(),
                actor_context=ACTOR,
                price_cents=20_000,
                company_choice="new",
                contact_choice="new",
            )

    assert result["idempotent"] is True
    assert result["source_record"]["id"] == winner_source["id"]
    assert len(shared_db._committed["companies"]) == 1
    assert len(shared_db._committed["contacts"]) == 1
    assert len(shared_db._committed["activities"]) == 0
    assert len(shared_db._committed["audit_events"]) == 0


def test_unrelated_unique_violation_is_not_swallowed() -> None:
    shared_db = _SharedBriefConversionDatabase()
    service = _conversion_service(shared_db)
    conn = _make_conn(shared_db)

    def create_contact(
        conn: MagicMock,
        *,
        email: str,
        full_name: str,
        company_id: UUID,
    ) -> dict[str, Any]:
        raise _unique_violation(
            "duplicate email",
            constraint_name="contacts_email_unique",
            table_name="contacts",
        )

    service._repos.contacts.create = create_contact  # type: ignore[method-assign]

    with patch("app.crm_service.audit_service.record_brief_convert"):
        with pytest.raises(psycopg.errors.UniqueViolation, match="duplicate email"):
            service.convert_project_brief(
                conn,
                brief=_brief(),
                actor_context=ACTOR,
                price_cents=20_000,
                company_choice="new",
                contact_choice="new",
            )

    assert shared_db._committed["source_records"] == {}
    assert shared_db._committed["companies"] == {}
