"""Real-PostgreSQL three-state CRM patch contracts (#279).

Proves omitted / explicit-NULL / value semantics for every nullable
CompanyUpdate, ContactUpdate, and pipeline patch field through the full
form-model-service-repository-PostgreSQL path, including audit before/after
semantics and shared-transaction rollback.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch
from uuid import UUID

import psycopg
import pytest

from app import audit_service
from app.acquisition_pipeline import PipelineNextActionUpdate, pipeline_summary
from app.actor_context import ActorContext
from app.companies import CompanyUpdate, company_audit_summary
from app.contacts import ContactUpdate, contact_audit_summary
from app.crm_service import CrmService
from app.repositories.postgres import (
    PostgresAcquisitionDashboardRepository,
    PostgresCompanyRepository,
    PostgresContactRepository,
    PostgresPipelineRepository,
)

ACTOR = ActorContext(actor="operator", correlation_id="corr-clear-contract")


# --------------------------------------------------------------------------- #
# Field matrices — every nullable patch field named by the issue acceptance   #
# --------------------------------------------------------------------------- #

COMPANY_NULLABLE_FIELDS: list[tuple[str, Any, Any]] = [
    ("website", "https://seed.example", "https://new.example"),
    ("domain", "seed.example", "new.example"),
    ("category", "fintech", "ai_infrastructure"),
    ("stage", "seed", "series_a"),
    ("headcount_estimate", 42, 99),
    ("funding_summary", "Seed round", "Series A closed"),
    ("target_status", "target", "watching"),
    ("last_verified_at", date(2025, 1, 15), date(2025, 6, 1)),
    ("notes", "Warm intro path", "Updated notes"),
]

CONTACT_NULLABLE_FIELDS: list[tuple[str, Any, Any]] = [
    ("title", "CTO", "VP Engineering"),
    ("profile_url", "https://linkedin.com/in/seed", "https://linkedin.com/in/new"),
    ("email", "seed@example.com", "new@example.com"),
    ("email_permission", "unknown", "permitted"),
    ("company_id", "linked", "other"),
    ("last_interaction_at", date(2025, 2, 1), date(2025, 7, 1)),
    ("relationship_strength", "cold", "warm"),
    ("notes", "Met at conference", "Follow-up scheduled"),
    ("buying_roles", ["founder"], ["technical_buyer"]),
]

PIPELINE_NULLABLE_FIELDS: list[tuple[str, Any, Any, str]] = [
    # field, seed, replacement, patch style: "next_action" | "fields" | "clear_flag"
    ("next_action", "Call founder", "Send proposal", "next_action"),
    ("next_action_due_at", datetime(2025, 3, 1, tzinfo=timezone.utc), datetime(2025, 8, 1, tzinfo=timezone.utc), "next_action"),
    ("pipeline_owner", "Alex", "Jordan", "next_action"),
    ("expected_value_cents", 25_000, 75_000, "next_action"),
    ("pipeline_loss_reason", "Budget cut", "Timing", "fields"),
    ("pipeline_nurture_reason", "Not ready", "Revisit Q4", "fields"),
]


def _create_pipeline_company(
    companies: PostgresCompanyRepository,
    pipeline: PostgresPipelineRepository,
    conn: psycopg.Connection,
    *,
    name: str,
    stage: str = "qualified",
) -> dict[str, Any]:
    company = companies.create(conn, name=name)
    pipeline.update_pipeline_fields(conn, company["id"], pipeline_stage=stage)
    return company


def _fresh_field(
    connect: Callable[..., psycopg.Connection],
    *,
    table: str,
    row_id: UUID,
    column: str,
) -> Any:
    verifier = connect()
    row = verifier.execute(
        f"SELECT {column} FROM {table} WHERE id = %s",
        (row_id,),
    ).fetchone()
    assert row is not None
    return row[column]


def _assert_fresh(
    connect: Callable[..., psycopg.Connection],
    *,
    table: str,
    row_id: UUID,
    column: str,
    expected: Any,
) -> None:
    actual = _fresh_field(connect, table=table, row_id=row_id, column=column)
    if isinstance(expected, datetime) and actual is not None:
        assert actual == expected
    elif isinstance(expected, date) and actual is not None:
        assert actual == expected
    elif column == "buying_roles":
        assert list(actual or []) == list(expected or [])
    else:
        assert actual == expected


def _latest_audit(
    connect: Callable[..., psycopg.Connection],
    *,
    action: str,
) -> dict[str, Any]:
    verifier = connect()
    row = verifier.execute(
        """
        SELECT action, entity_type, summary_before, summary_after, metadata
        FROM audit_events
        WHERE action = %s
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (action,),
    ).fetchone()
    assert row is not None
    before = row["summary_before"]
    after = row["summary_after"]
    metadata = row["metadata"]
    if isinstance(before, str):
        before = json.loads(before)
    if isinstance(after, str):
        after = json.loads(after)
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    return {
        "action": row["action"],
        "entity_type": row["entity_type"],
        "summary_before": before,
        "summary_after": after,
        "metadata": metadata,
    }


def _audit_field_value(summary: dict[str, Any] | None, field: str) -> Any:
    assert summary is not None
    value = summary[field]
    if field == "last_verified_at" and isinstance(value, str):
        return date.fromisoformat(value)
    if field == "last_interaction_at" and isinstance(value, str):
        return date.fromisoformat(value)
    if field == "next_action_due_at" and isinstance(value, str):
        return datetime.fromisoformat(value)
    if field == "company_id" and value is not None:
        return str(value)
    return value


# --------------------------------------------------------------------------- #
# Company — omit / replace / clear with fresh-connection reads                #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("field,seed_value,replacement", COMPANY_NULLABLE_FIELDS)
def test_company_nullable_field_three_state_contract(
    migrated_conn: psycopg.Connection,
    connect: Callable[..., psycopg.Connection],
    field: str,
    seed_value: Any,
    replacement: Any,
) -> None:
    repo = PostgresCompanyRepository()
    service = CrmService()
    created = repo.create(migrated_conn, name="Acme", **{field: seed_value})
    company_id = created["id"]
    migrated_conn.commit()

    # Omit: only the required name is supplied; the stored value stays put.
    service.update_company(
        migrated_conn,
        company_id,
        company=CompanyUpdate(name="Acme"),
        actor_context=ACTOR,
    )
    migrated_conn.commit()
    _assert_fresh(
        connect,
        table="companies",
        row_id=company_id,
        column=field,
        expected=seed_value,
    )

    # Replace: a non-empty supplied value overwrites the column.
    service.update_company(
        migrated_conn,
        company_id,
        company=CompanyUpdate(name="Acme", **{field: replacement}),
        actor_context=ACTOR,
    )
    migrated_conn.commit()
    _assert_fresh(
        connect,
        table="companies",
        row_id=company_id,
        column=field,
        expected=replacement,
    )

    # Clear: an explicit None writes SQL NULL and survives a fresh connection.
    service.update_company(
        migrated_conn,
        company_id,
        company=CompanyUpdate(name="Acme", **{field: None}),
        actor_context=ACTOR,
    )
    migrated_conn.commit()
    _assert_fresh(
        connect,
        table="companies",
        row_id=company_id,
        column=field,
        expected=None,
    )


# --------------------------------------------------------------------------- #
# Contact — omit / replace / clear (including company_id disassociation)      #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("field,seed_value,replacement", CONTACT_NULLABLE_FIELDS)
def test_contact_nullable_field_three_state_contract(
    migrated_conn: psycopg.Connection,
    connect: Callable[..., psycopg.Connection],
    field: str,
    seed_value: Any,
    replacement: Any,
) -> None:
    companies = PostgresCompanyRepository()
    contacts = PostgresContactRepository()
    service = CrmService()
    company = companies.create(migrated_conn, name="Employer Co")
    company_id = company["id"]
    other_company = companies.create(migrated_conn, name="Other Co")
    other_company_id = other_company["id"]

    seed_kwargs: dict[str, Any] = {field: seed_value}
    if field == "company_id":
        seed_kwargs[field] = company_id
    created = contacts.create(migrated_conn, full_name="Dana Lead", **seed_kwargs)
    contact_id = created["id"]
    migrated_conn.commit()

    # Omit
    service.update_contact(
        migrated_conn,
        contact_id,
        contact=ContactUpdate(full_name="Dana Lead"),
        actor_context=ACTOR,
    )
    migrated_conn.commit()
    expected_after_omit = company_id if field == "company_id" else seed_value
    _assert_fresh(
        connect,
        table="contacts",
        row_id=contact_id,
        column=field,
        expected=expected_after_omit,
    )

    # Replace
    replace_kwargs: dict[str, Any] = {field: replacement}
    if field == "company_id":
        replace_kwargs[field] = other_company_id
    service.update_contact(
        migrated_conn,
        contact_id,
        contact=ContactUpdate(full_name="Dana Lead", **replace_kwargs),
        actor_context=ACTOR,
    )
    migrated_conn.commit()
    expected_after_replace = other_company_id if field == "company_id" else replacement
    _assert_fresh(
        connect,
        table="contacts",
        row_id=contact_id,
        column=field,
        expected=expected_after_replace,
    )

    # Clear / disassociate — buying_roles clear to [] rather than None.
    clear_kwargs: dict[str, Any] = {field: [] if field == "buying_roles" else None}
    service.update_contact(
        migrated_conn,
        contact_id,
        contact=ContactUpdate(full_name="Dana Lead", **clear_kwargs),
        actor_context=ACTOR,
    )
    migrated_conn.commit()
    expected_clear = [] if field == "buying_roles" else None
    _assert_fresh(
        connect,
        table="contacts",
        row_id=contact_id,
        column=field,
        expected=expected_clear,
    )


# --------------------------------------------------------------------------- #
# Pipeline — omit / replace / clear with fresh-connection reads               #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "field,seed_value,replacement,patch_style",
    PIPELINE_NULLABLE_FIELDS,
)
def test_pipeline_nullable_field_three_state_contract(
    migrated_conn: psycopg.Connection,
    connect: Callable[..., psycopg.Connection],
    field: str,
    seed_value: Any,
    replacement: Any,
    patch_style: str,
) -> None:
    companies = PostgresCompanyRepository()
    pipeline = PostgresPipelineRepository()
    service = CrmService()
    company = _create_pipeline_company(companies, pipeline, migrated_conn, name="Pipeline Co")
    company_id = company["id"]
    pipeline.update_pipeline_fields(migrated_conn, company_id, **{field: seed_value})
    migrated_conn.commit()

    def _apply_patch(**kwargs: Any) -> None:
        if patch_style == "next_action":
            service.update_pipeline_next_action(
                migrated_conn,
                actor_context=ACTOR,
                company_id=company_id,
                update=PipelineNextActionUpdate(**kwargs),
            )
        else:
            pipeline.update_pipeline_fields(migrated_conn, company_id, **kwargs)

    # Omit — patch an unrelated owner field only.
    if field != "pipeline_owner":
        _apply_patch(pipeline_owner="Keeper")
    else:
        _apply_patch(next_action="Keep action")
    migrated_conn.commit()
    _assert_fresh(
        connect,
        table="companies",
        row_id=company_id,
        column=field,
        expected=seed_value,
    )

    # Replace
    _apply_patch(**{field: replacement})
    migrated_conn.commit()
    _assert_fresh(
        connect,
        table="companies",
        row_id=company_id,
        column=field,
        expected=replacement,
    )

    # Clear
    if patch_style == "next_action":
        _apply_patch(**{field: None})
    else:
        pipeline.update_pipeline_fields(migrated_conn, company_id, **{field: None})
    migrated_conn.commit()
    _assert_fresh(
        connect,
        table="companies",
        row_id=company_id,
        column=field,
        expected=None,
    )


def test_pipeline_reason_clear_via_flag_contract(
    migrated_conn: psycopg.Connection,
    connect: Callable[..., psycopg.Connection],
) -> None:
    """Stage-driven reason resets use clear_* flags (UNSET + flag → SQL NULL)."""
    companies = PostgresCompanyRepository()
    pipeline = PostgresPipelineRepository()
    company = _create_pipeline_company(companies, pipeline, migrated_conn, name="Reason Co", stage="lost")
    company_id = company["id"]
    pipeline.update_pipeline_fields(
        migrated_conn,
        company_id,
        pipeline_loss_reason="Budget cut",
        pipeline_nurture_reason="Not ready",
    )
    migrated_conn.commit()

    pipeline.update_pipeline_fields(
        migrated_conn,
        company_id,
        next_action="Follow up",
        clear_loss_reason=True,
        clear_nurture_reason=True,
    )
    migrated_conn.commit()

    _assert_fresh(
        connect,
        table="companies",
        row_id=company_id,
        column="pipeline_loss_reason",
        expected=None,
    )
    _assert_fresh(
        connect,
        table="companies",
        row_id=company_id,
        column="pipeline_nurture_reason",
        expected=None,
    )
    _assert_fresh(
        connect,
        table="companies",
        row_id=company_id,
        column="next_action",
        expected="Follow up",
    )


# --------------------------------------------------------------------------- #
# Audit — clear / replace / unchanged semantics                               #
# --------------------------------------------------------------------------- #


def test_company_update_audit_distinguishes_clear_replace_unchanged(
    migrated_conn: psycopg.Connection,
    connect: Callable[..., psycopg.Connection],
) -> None:
    repo = PostgresCompanyRepository()
    service = CrmService()
    company = repo.create(
        migrated_conn,
        name="Audit Co",
        notes="Keep me",
        funding_summary="Clear me",
    )
    company_id = company["id"]

    service.update_company(
        migrated_conn,
        company_id,
        company=CompanyUpdate(name="Audit Co", funding_summary=None),
        actor_context=ACTOR,
    )
    migrated_conn.commit()

    event = _latest_audit(connect, action=audit_service.ACTION_COMPANY_UPDATE)
    assert event["entity_type"] == "company"
    before = event["summary_before"]
    after = event["summary_after"]
    assert before["has_notes"] is True
    assert after["has_notes"] is True
    assert before["has_funding_summary"] is True
    assert after["has_funding_summary"] is False
    assert event["metadata"]["changed_fields"] == ["has_funding_summary"]

    service.update_company(
        migrated_conn,
        company_id,
        company=CompanyUpdate(name="Audit Co", notes="Replaced"),
        actor_context=ACTOR,
    )
    migrated_conn.commit()

    event = _latest_audit(connect, action=audit_service.ACTION_COMPANY_UPDATE)
    assert event["summary_before"]["has_notes"] is True
    assert event["summary_after"]["has_notes"] is True
    assert "notes" not in (event["summary_before"] or {})
    assert "email" not in (event["summary_before"] or {})
    assert "email" not in (event["summary_after"] or {})


def test_contact_update_audit_distinguishes_clear_replace_unchanged(
    migrated_conn: psycopg.Connection,
    connect: Callable[..., psycopg.Connection],
) -> None:
    companies = PostgresCompanyRepository()
    contacts = PostgresContactRepository()
    service = CrmService()
    company = companies.create(migrated_conn, name="Employer")
    contact = contacts.create(
        migrated_conn,
        full_name="Audit Contact",
        email="secret@example.com",
        title="Keep me",
        notes="Clear me",
        company_id=company["id"],
    )
    contact_id = contact["id"]

    service.update_contact(
        migrated_conn,
        contact_id,
        contact=ContactUpdate(full_name="Audit Contact", notes=None),
        actor_context=ACTOR,
    )
    migrated_conn.commit()

    event = _latest_audit(connect, action=audit_service.ACTION_CONTACT_UPDATE)
    assert event["entity_type"] == "contact"
    before = event["summary_before"]
    after = event["summary_after"]
    assert before["title"] == "Keep me"
    assert after["title"] == "Keep me"
    assert before["has_notes"] is True
    assert after["has_notes"] is False
    assert event["metadata"]["changed_fields"] == ["has_notes"]
    assert "email" not in before
    assert "email" not in after
    assert "notes" not in before

    service.update_contact(
        migrated_conn,
        contact_id,
        contact=ContactUpdate(full_name="Audit Contact", title="Replaced"),
        actor_context=ACTOR,
    )
    migrated_conn.commit()

    event = _latest_audit(connect, action=audit_service.ACTION_CONTACT_UPDATE)
    assert event["summary_before"]["title"] == "Keep me"
    assert event["summary_after"]["title"] == "Replaced"


def test_pipeline_update_audit_distinguishes_clear_replace_unchanged(
    migrated_conn: psycopg.Connection,
    connect: Callable[..., psycopg.Connection],
) -> None:
    companies = PostgresCompanyRepository()
    pipeline = PostgresPipelineRepository()
    service = CrmService()
    company = _create_pipeline_company(companies, pipeline, migrated_conn, name="Pipe Audit Co")
    company_id = company["id"]
    due = datetime(2025, 4, 1, tzinfo=timezone.utc)
    pipeline.update_pipeline_fields(
        migrated_conn,
        company_id,
        next_action="Keep me",
        next_action_due_at=due,
        pipeline_owner="Clear me",
        expected_value_cents=10_000,
    )

    service.update_pipeline_next_action(
        migrated_conn,
        actor_context=ACTOR,
        company_id=company_id,
        update=PipelineNextActionUpdate(pipeline_owner=""),
    )
    migrated_conn.commit()

    event = _latest_audit(connect, action=audit_service.ACTION_PIPELINE_UPDATE)
    before = event["summary_before"]
    after = event["summary_after"]
    assert before["next_action"] == "Keep me"
    assert after["next_action"] == "Keep me"
    assert before["pipeline_owner"] == "Clear me"
    assert after["pipeline_owner"] is None
    assert before["next_action_due_at"] == due.isoformat()
    assert after["next_action_due_at"] == due.isoformat()

    service.update_pipeline_next_action(
        migrated_conn,
        actor_context=ACTOR,
        company_id=company_id,
        update=PipelineNextActionUpdate(next_action="Replaced"),
    )
    migrated_conn.commit()

    event = _latest_audit(connect, action=audit_service.ACTION_PIPELINE_UPDATE)
    assert event["summary_before"]["next_action"] == "Keep me"
    assert event["summary_after"]["next_action"] == "Replaced"


# --------------------------------------------------------------------------- #
# Shared transaction rollback on repository / audit failures                  #
# --------------------------------------------------------------------------- #


def test_company_update_rolls_back_on_audit_failure(
    migrated_conn: psycopg.Connection,
    connect: Callable[..., psycopg.Connection],
) -> None:
    repo = PostgresCompanyRepository()
    service = CrmService()
    company = repo.create(migrated_conn, name="Rollback Co", notes="Before")
    company_id = company["id"]
    migrated_conn.commit()

    with patch(
        "app.crm_service.audit_service.record_company_update",
        side_effect=RuntimeError("audit sink unavailable"),
    ):
        with pytest.raises(RuntimeError, match="audit sink unavailable"):
            service.update_company(
                migrated_conn,
                company_id,
                company=CompanyUpdate(name="Rollback Co", notes="After"),
                actor_context=ACTOR,
            )

    _assert_fresh(
        connect,
        table="companies",
        row_id=company_id,
        column="notes",
        expected="Before",
    )
    verifier = connect()
    assert verifier.execute("SELECT COUNT(*) AS n FROM audit_events").fetchone()["n"] == 0


def test_contact_update_rolls_back_on_audit_failure(
    migrated_conn: psycopg.Connection,
    connect: Callable[..., psycopg.Connection],
) -> None:
    contacts = PostgresContactRepository()
    service = CrmService()
    contact = contacts.create(migrated_conn, full_name="Rollback Contact", notes="Before")
    contact_id = contact["id"]
    migrated_conn.commit()

    with patch(
        "app.crm_service.audit_service.record_contact_update",
        side_effect=RuntimeError("audit sink unavailable"),
    ):
        with pytest.raises(RuntimeError, match="audit sink unavailable"):
            service.update_contact(
                migrated_conn,
                contact_id,
                contact=ContactUpdate(full_name="Rollback Contact", notes="After"),
                actor_context=ACTOR,
            )

    _assert_fresh(
        connect,
        table="contacts",
        row_id=contact_id,
        column="notes",
        expected="Before",
    )
    verifier = connect()
    assert verifier.execute("SELECT COUNT(*) AS n FROM audit_events").fetchone()["n"] == 0


def test_pipeline_update_rolls_back_on_audit_failure(
    migrated_conn: psycopg.Connection,
    connect: Callable[..., psycopg.Connection],
) -> None:
    companies = PostgresCompanyRepository()
    pipeline = PostgresPipelineRepository()
    service = CrmService()
    company = _create_pipeline_company(companies, pipeline, migrated_conn, name="Pipe Rollback")
    company_id = company["id"]
    pipeline.update_pipeline_fields(
        migrated_conn,
        company_id,
        next_action="Before",
    )
    migrated_conn.commit()

    with patch(
        "app.crm_service.audit_service.record_pipeline_update",
        side_effect=RuntimeError("audit sink unavailable"),
    ):
        with pytest.raises(RuntimeError, match="audit sink unavailable"):
            service.update_pipeline_next_action(
                migrated_conn,
                actor_context=ACTOR,
                company_id=company_id,
                update=PipelineNextActionUpdate(next_action="After"),
            )

    _assert_fresh(
        connect,
        table="companies",
        row_id=company_id,
        column="next_action",
        expected="Before",
    )
    verifier = connect()
    assert verifier.execute("SELECT COUNT(*) AS n FROM audit_events").fetchone()["n"] == 0


def test_company_update_rolls_back_on_repository_failure(
    migrated_conn: psycopg.Connection,
    connect: Callable[..., psycopg.Connection],
) -> None:
    repo = PostgresCompanyRepository()
    service = CrmService()
    company = repo.create(migrated_conn, name="Repo Rollback", notes="Before")
    company_id = company["id"]
    migrated_conn.commit()

    with patch.object(
        PostgresCompanyRepository,
        "update",
        side_effect=RuntimeError("repository write failed"),
    ):
        with pytest.raises(RuntimeError, match="repository write failed"):
            service.update_company(
                migrated_conn,
                company_id,
                company=CompanyUpdate(name="Repo Rollback", notes="After"),
                actor_context=ACTOR,
            )

    _assert_fresh(
        connect,
        table="companies",
        row_id=company_id,
        column="notes",
        expected="Before",
    )
    verifier = connect()
    assert verifier.execute("SELECT COUNT(*) AS n FROM audit_events").fetchone()["n"] == 0


# --------------------------------------------------------------------------- #
# Dashboard queries exclude cleared next-action values                        #
# --------------------------------------------------------------------------- #


def test_cleared_next_action_excluded_from_dashboard_lists(
    migrated_conn: psycopg.Connection,
    connect: Callable[..., psycopg.Connection],
) -> None:
    companies = PostgresCompanyRepository()
    pipeline = PostgresPipelineRepository()
    dashboard = PostgresAcquisitionDashboardRepository()
    service = CrmService()
    reference = datetime(2025, 5, 1, 12, 0, tzinfo=timezone.utc)
    overdue_due = reference - timedelta(days=2)
    upcoming_due = reference + timedelta(days=2)

    overdue_co = _create_pipeline_company(companies, pipeline, migrated_conn, name="Overdue Co")
    overdue_id = overdue_co["id"]
    pipeline.update_pipeline_fields(
        migrated_conn,
        overdue_id,
        next_action="Call",
        next_action_due_at=overdue_due,
        pipeline_owner="Alex",
        pipeline_loss_reason="Old loss",
        pipeline_nurture_reason="Old nurture",
    )

    upcoming_co = _create_pipeline_company(companies, pipeline, migrated_conn, name="Upcoming Co")
    upcoming_id = upcoming_co["id"]
    pipeline.update_pipeline_fields(
        migrated_conn,
        upcoming_id,
        next_action="Email",
        next_action_due_at=upcoming_due,
        pipeline_owner="Jordan",
    )
    migrated_conn.commit()

    overdue_before = {row["id"] for row in pipeline.list_overdue_next_actions(migrated_conn, reference=reference, limit=20)}
    upcoming_before = {
        row["id"]
        for row in pipeline.list_upcoming_next_actions(
            migrated_conn,
            reference=reference,
            window_end=reference + timedelta(days=7),
            limit=20,
        )
    }
    assert overdue_id in overdue_before
    assert upcoming_id in upcoming_before

    service.update_pipeline_next_action(
        migrated_conn,
        actor_context=ACTOR,
        company_id=overdue_id,
        update=PipelineNextActionUpdate(
            next_action="",
            next_action_due_at=None,
            pipeline_owner="",
        ),
    )
    pipeline.update_pipeline_fields(
        migrated_conn,
        overdue_id,
        pipeline_loss_reason=None,
        pipeline_nurture_reason=None,
    )
    service.update_pipeline_next_action(
        migrated_conn,
        actor_context=ACTOR,
        company_id=upcoming_id,
        update=PipelineNextActionUpdate(next_action="", next_action_due_at=None),
    )
    migrated_conn.commit()

    overdue_after = {row["id"] for row in pipeline.list_overdue_next_actions(migrated_conn, reference=reference, limit=20)}
    upcoming_after = {
        row["id"]
        for row in pipeline.list_upcoming_next_actions(
            migrated_conn,
            reference=reference,
            window_end=reference + timedelta(days=7),
            limit=20,
        )
    }
    without = {row["id"] for row in dashboard.list_companies_without_next_action(migrated_conn, limit=20)}

    assert overdue_id not in overdue_after
    assert upcoming_id not in upcoming_after
    assert overdue_id in without
    assert upcoming_id in without

    _assert_fresh(connect, table="companies", row_id=overdue_id, column="next_action", expected=None)
    _assert_fresh(connect, table="companies", row_id=overdue_id, column="next_action_due_at", expected=None)
    _assert_fresh(connect, table="companies", row_id=overdue_id, column="pipeline_owner", expected=None)
    _assert_fresh(connect, table="companies", row_id=overdue_id, column="pipeline_loss_reason", expected=None)
    _assert_fresh(connect, table="companies", row_id=overdue_id, column="pipeline_nurture_reason", expected=None)


# --------------------------------------------------------------------------- #
# Summary helpers mirror persisted values for contract assertions             #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("field,seed_value,replacement", COMPANY_NULLABLE_FIELDS)
def test_company_audit_summary_tracks_nullable_fields(
    field: str,
    seed_value: Any,
    replacement: Any,
) -> None:
    before = company_audit_summary({"name": "Acme", field: seed_value})
    after_clear = company_audit_summary({"name": "Acme", field: None})
    after_replace = company_audit_summary({"name": "Acme", field: replacement})
    assert _audit_field_value(before, field) == seed_value
    assert _audit_field_value(after_clear, field) is None
    assert _audit_field_value(after_replace, field) == replacement


@pytest.mark.parametrize("field,seed_value,replacement", CONTACT_NULLABLE_FIELDS)
def test_contact_audit_summary_omits_email(
    field: str,
    seed_value: Any,
    replacement: Any,
) -> None:
    payload = {
        "id": UUID("00000000-0000-0000-0000-000000000101"),
        "full_name": "Ada",
        field: seed_value,
        "email": "secret@example.com",
    }
    summary = contact_audit_summary(payload)
    assert "email" not in summary
    if field in summary:
        actual = _audit_field_value(summary, field)
        expected = seed_value
        if field == "company_id" and expected is not None:
            expected = str(expected)
        assert actual == expected


def test_pipeline_summary_matches_repository_columns() -> None:
    due = datetime(2025, 5, 1, tzinfo=timezone.utc)
    row = {
        "pipeline_stage": "qualified",
        "next_action": "Call",
        "next_action_due_at": due,
        "pipeline_owner": "Alex",
        "expected_value_cents": 42_000,
        "pipeline_loss_reason": "Budget",
        "pipeline_nurture_reason": None,
    }
    summary = pipeline_summary(row)
    assert summary["next_action_due_at"] == due.isoformat()
    assert summary["pipeline_loss_reason"] == "Budget"
