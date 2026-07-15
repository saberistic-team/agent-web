"""CRM service boundary — orchestrates repositories without embedding SQL."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import psycopg

from app import audit_service
from app.acquisition_pipeline import (
    PipelineActivityCreate,
    PipelineNextActionUpdate,
    PipelineStageChange,
    PipelineTransitionError,
    assess_stage_transition,
    pipeline_summary,
)
from app.actor_context import ActorContext
from app.brief_conversion import (
    BriefConversionValidationError,
    build_conversion_proposal,
    safe_conversion_payload,
)
from app.companies import CompanyCreate, CompanyUpdate, find_domain_duplicate_warnings, normalize_domain
from app.contacts import (
    ContactCreate,
    ContactUpdate,
    find_email_duplicate_warnings,
    find_name_company_duplicate_warnings,
    find_profile_url_duplicate_warnings,
)
from app.crm_uow import crm_transaction
from app.pipeline import initial_pipeline_stage_for_brief_status, pipeline_stage_label, validate_stage
from app.repositories import (
    ActivityRepository,
    AdminUserRepository,
    CompanyRepository,
    ContactRepository,
    PipelineRepository,
    PostgresActivityRepository,
    PostgresAdminUserRepository,
    PostgresCompanyRepository,
    PostgresContactRepository,
    PostgresPipelineRepository,
    PostgresResearchRecordRepository,
    PostgresSourceRecordRepository,
    ResearchRecordRepository,
    SourceRecordRepository,
)


@dataclass(frozen=True)
class CrmRepositories:
    companies: CompanyRepository
    contacts: ContactRepository
    source_records: SourceRecordRepository
    activities: ActivityRepository
    research_records: ResearchRecordRepository
    admin_users: AdminUserRepository
    pipeline: PipelineRepository


def default_crm_repositories() -> CrmRepositories:
    return CrmRepositories(
        companies=PostgresCompanyRepository(),
        contacts=PostgresContactRepository(),
        source_records=PostgresSourceRecordRepository(),
        activities=PostgresActivityRepository(),
        research_records=PostgresResearchRecordRepository(),
        admin_users=PostgresAdminUserRepository(),
        pipeline=PostgresPipelineRepository(),
    )


class CrmService:
    """Thin service layer for admin/import/discovery callers."""

    def __init__(self, repos: CrmRepositories | None = None) -> None:
        self._repos = repos or default_crm_repositories()

    def record_company_with_contact(
        self,
        conn: psycopg.Connection,
        *,
        company_name: str,
        website: str | None,
        contact_email: str,
        contact_name: str | None = None,
    ) -> dict[str, Any]:
        with crm_transaction(conn):
            company = self._repos.companies.create(
                conn,
                name=company_name,
                website=website,
            )
            contact = self._repos.contacts.create(
                conn,
                full_name=contact_name or contact_email.split("@", 1)[0],
                email=contact_email,
                company_id=UUID(str(company["id"])),
            )
        return {"company": company, "contact": contact}

    def record_activity_for_company(
        self,
        conn: psycopg.Connection,
        *,
        company_id: UUID,
        activity_type: str,
        summary: str,
        contact_id: UUID | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with crm_transaction(conn):
            activity = self._repos.activities.create(
                conn,
                activity_type=activity_type,
                summary=summary,
                company_id=company_id,
                contact_id=contact_id,
                metadata=metadata,
            )
        return activity

    def link_project_brief_source(
        self,
        conn: psycopg.Connection,
        *,
        brief_id: int,
        company_id: UUID,
        contact_id: UUID,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with crm_transaction(conn):
            record = self._repos.source_records.create(
                conn,
                source_type="project_brief",
                external_id=str(brief_id),
                company_id=company_id,
                contact_id=contact_id,
                payload=payload,
            )
        return record

    def get_project_brief_source(
        self,
        conn: psycopg.Connection,
        brief_id: int,
    ) -> dict[str, Any] | None:
        return self._repos.source_records.get_by_source(
            conn,
            source_type="project_brief",
            external_id=str(brief_id),
        )

    def get_brief_conversion_state(
        self,
        conn: psycopg.Connection,
        brief_id: int,
    ) -> dict[str, Any] | None:
        source = self.get_project_brief_source(conn, brief_id)
        if source is None:
            return None
        return self._conversion_result_from_source(conn, source, idempotent=True)

    def find_brief_conversion_matches(
        self,
        conn: psycopg.Connection,
        brief: dict[str, Any],
        *,
        price_cents: int,
    ) -> dict[str, Any]:
        proposal = build_conversion_proposal(brief, price_cents=price_cents)
        domain = proposal.get("domain")
        email = proposal["contact_email"]
        companies = (
            self._repos.companies.find_by_domain(conn, str(domain))
            if domain
            else []
        )
        contact = self._repos.contacts.get_by_email(conn, email)
        contacts = [contact] if contact else []
        return {
            "proposal": proposal,
            "company_matches": companies,
            "contact_matches": contacts,
        }

    def convert_project_brief(
        self,
        conn: psycopg.Connection,
        *,
        brief: dict[str, Any],
        actor_context: ActorContext,
        price_cents: int,
        company_choice: str,
        contact_choice: str,
        selected_company_id: UUID | None = None,
        selected_contact_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Create or link CRM records and pipeline state for one project brief."""
        brief_id = int(brief["id"])
        existing = self.get_project_brief_source(conn, brief_id)
        if existing is not None:
            return self._conversion_result_from_source(conn, existing, idempotent=True)

        proposal = build_conversion_proposal(brief, price_cents=price_cents)
        pipeline_stage = initial_pipeline_stage_for_brief_status(str(brief.get("status", "")))
        validate_stage(pipeline_stage)
        domain = proposal.get("domain")
        email = proposal["contact_email"]
        company_matches = (
            self._repos.companies.find_by_domain(conn, str(domain))
            if domain
            else []
        )
        contact_match = self._repos.contacts.get_by_email(conn, email)
        self._validate_conversion_choices(
            company_choice=company_choice,
            contact_choice=contact_choice,
            company_matches=company_matches,
            contact_match=contact_match,
            selected_company_id=selected_company_id,
            selected_contact_id=selected_contact_id,
        )

        with crm_transaction(conn):
            race = self.get_project_brief_source(conn, brief_id)
            if race is not None:
                return self._conversion_result_from_source(conn, race, idempotent=True)

            expected_value = proposal.get("expected_value")
            expected_value_cents = (
                int(round(float(expected_value) * 100))
                if expected_value is not None
                else None
            )

            if company_choice == "existing":
                assert selected_company_id is not None
                company = self._repos.companies.get_by_id(conn, selected_company_id)
                if company is None:
                    raise BriefConversionValidationError("Selected company was not found.")
                from_stage = company.get("pipeline_stage")
                if from_stage is not None:
                    from_stage = str(from_stage)
            else:
                from_stage = "researching"
                company = self._repos.companies.create(
                    conn,
                    name=str(proposal["company_name"]),
                    website=str(proposal["website"]),
                    domain=domain,
                )

            if contact_choice == "existing":
                assert selected_contact_id is not None
                contact = self._repos.contacts.get_by_id(conn, selected_contact_id)
                if contact is None:
                    raise BriefConversionValidationError("Selected contact was not found.")
            else:
                contact = self._repos.contacts.create(
                    conn,
                    email=email,
                    full_name=proposal.get("contact_name") or email.split("@", 1)[0],
                    company_id=UUID(str(company["id"])),
                )

            updated = self._repos.pipeline.update_pipeline_fields(
                conn,
                UUID(str(company["id"])),
                pipeline_stage=pipeline_stage,
                expected_value_cents=expected_value_cents,
            )
            if updated is not None:
                company = updated

            if from_stage != pipeline_stage:
                self._repos.pipeline.record_stage_history(
                    conn,
                    company_id=UUID(str(company["id"])),
                    from_stage=from_stage,
                    to_stage=pipeline_stage,
                    changed_by=actor_context.actor,
                    metadata={"brief_id": brief_id, "reason": "Brief conversion"},
                )

            payload = {
                **safe_conversion_payload(brief),
                "pipeline_stage": pipeline_stage,
            }
            source_record = self._repos.source_records.create(
                conn,
                source_type="project_brief",
                external_id=str(brief_id),
                company_id=UUID(str(company["id"])),
                contact_id=UUID(str(contact["id"])),
                payload=payload,
            )
            self._repos.activities.create(
                conn,
                activity_type="status_change",
                summary=(
                    f"Added brief #{brief_id} to pipeline at "
                    f"{pipeline_stage_label(pipeline_stage)}"
                ),
                company_id=UUID(str(company["id"])),
                contact_id=UUID(str(contact["id"])),
                source_record_id=UUID(str(source_record["id"])),
                metadata={"brief_id": brief_id, "pipeline_stage": pipeline_stage},
            )
            audit_service.record_brief_convert(
                conn,
                actor_context=actor_context,
                brief_id=str(brief_id),
                summary_after={
                    "brief_id": brief_id,
                    "brief_status": brief.get("status"),
                    "company_id": str(company["id"]),
                    "contact_id": str(contact["id"]),
                    "pipeline_stage": pipeline_stage,
                    "outcome": "linked",
                },
            )

        return {
            "idempotent": False,
            "brief_id": brief_id,
            "company": company,
            "contact": contact,
            "source_record": source_record,
            "pipeline_stage": pipeline_stage,
        }

    def _conversion_result_from_source(
        self,
        conn: psycopg.Connection,
        source_record: dict[str, Any],
        *,
        idempotent: bool,
    ) -> dict[str, Any]:
        company_id = source_record.get("company_id")
        contact_id = source_record.get("contact_id")
        company = (
            self._repos.companies.get_by_id(conn, UUID(str(company_id)))
            if company_id
            else None
        )
        contact = (
            self._repos.contacts.get_by_id(conn, UUID(str(contact_id)))
            if contact_id
            else None
        )
        payload = source_record.get("payload") or {}
        pipeline_stage = payload.get("pipeline_stage") or (
            company.get("pipeline_stage") if company else None
        )
        return {
            "idempotent": idempotent,
            "brief_id": int(source_record.get("external_id") or 0),
            "company": company,
            "contact": contact,
            "source_record": source_record,
            "pipeline_stage": pipeline_stage,
        }

    def _validate_conversion_choices(
        self,
        *,
        company_choice: str,
        contact_choice: str,
        company_matches: list[dict[str, Any]],
        contact_match: dict[str, Any] | None,
        selected_company_id: UUID | None,
        selected_contact_id: UUID | None,
    ) -> None:
        if company_choice not in {"new", "existing"}:
            raise BriefConversionValidationError("Choose whether to create or link a company.")
        if contact_choice not in {"new", "existing"}:
            raise BriefConversionValidationError("Choose whether to create or link a contact.")

        if company_matches and company_choice == "existing":
            if selected_company_id is None:
                raise BriefConversionValidationError(
                    "Select an existing company match or choose to create a new company."
                )
            allowed = {UUID(str(row["id"])) for row in company_matches}
            if selected_company_id not in allowed:
                raise BriefConversionValidationError("Selected company does not match the brief domain.")

        if not company_matches and company_choice == "existing":
            raise BriefConversionValidationError("No existing company matches this domain.")

        if contact_match is None and contact_choice == "existing":
            raise BriefConversionValidationError("No existing contact matches this email.")

        if contact_match is not None and contact_choice == "existing":
            if selected_contact_id is None:
                raise BriefConversionValidationError(
                    "Select the existing contact match or choose to create a new contact."
                )
            if selected_contact_id != UUID(str(contact_match["id"])):
                raise BriefConversionValidationError("Selected contact does not match the brief email.")
        elif contact_match is not None and contact_choice == "new":
            raise BriefConversionValidationError(
                "A contact with this email already exists — link the existing contact."
            )

        if company_choice == "existing" and selected_company_id is None:
            raise BriefConversionValidationError("A company selection is required.")
        if contact_choice == "existing" and selected_contact_id is None:
            raise BriefConversionValidationError("A contact selection is required.")

        if (
            company_choice == "existing"
            and contact_choice == "existing"
            and selected_company_id is not None
            and selected_contact_id is not None
        ):
            contact = contact_match
            if contact is None:
                raise BriefConversionValidationError("Selected contact was not found.")
            linked_company = contact.get("company_id")
            if linked_company is not None and UUID(str(linked_company)) != selected_company_id:
                raise BriefConversionValidationError(
                    "Selected contact belongs to a different company — adjust your selection."
                )

    def get_admin_user_by_email(
        self,
        conn: psycopg.Connection,
        email: str,
    ) -> dict[str, Any] | None:
        return self._repos.admin_users.get_by_email(conn, email)

    def list_companies(
        self,
        conn: psycopg.Connection,
        *,
        limit: int = 100,
        query: str | None = None,
        category: str | None = None,
        stage: str | None = None,
        target_status: str | None = None,
        freshness: str | None = None,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        return self._repos.companies.list_all(
            conn,
            limit=limit,
            query=query,
            category=category,
            stage=stage,
            target_status=target_status,
            freshness=freshness,
            include_archived=include_archived,
        )

    def get_company(
        self,
        conn: psycopg.Connection,
        company_id: UUID,
    ) -> dict[str, Any] | None:
        return self._repos.companies.get_by_id(conn, company_id)

    def create_company(
        self,
        conn: psycopg.Connection,
        *,
        company: CompanyCreate,
    ) -> dict[str, Any]:
        with crm_transaction(conn):
            duplicates = self._repos.companies.find_by_domain(conn, company.domain) if company.domain else []
            created = self._repos.companies.create(conn, **company.model_dump())
        return {
            "company": created,
            "duplicate_warnings": find_domain_duplicate_warnings(
                duplicates, domain=company.domain
            ),
        }

    def update_company(
        self,
        conn: psycopg.Connection,
        company_id: UUID,
        *,
        company: CompanyUpdate,
    ) -> dict[str, Any] | None:
        with crm_transaction(conn):
            duplicates = (
                self._repos.companies.find_by_domain(
                    conn, company.domain, exclude_company_id=company_id
                )
                if company.domain
                else []
            )
            updated = self._repos.companies.update(
                conn, company_id, **company.model_dump(exclude_none=True)
            )
        if updated is None:
            return None
        return {
            "company": updated,
            "duplicate_warnings": find_domain_duplicate_warnings(
                duplicates, domain=company.domain, exclude_company_id=company_id
            ),
        }

    def archive_company(
        self, conn: psycopg.Connection, company_id: UUID
    ) -> dict[str, Any] | None:
        with crm_transaction(conn):
            return self._repos.companies.archive(conn, company_id)

    def restore_company(
        self, conn: psycopg.Connection, company_id: UUID
    ) -> dict[str, Any] | None:
        with crm_transaction(conn):
            return self._repos.companies.restore(conn, company_id)

    def list_contacts_for_company(
        self,
        conn: psycopg.Connection,
        company_id: UUID,
        *,
        limit: int = 100,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        return self._repos.contacts.list_for_company(
            conn, company_id, limit=limit, include_archived=include_archived
        )

    def list_contacts(
        self,
        conn: psycopg.Connection,
        *,
        limit: int = 100,
        query: str | None = None,
        company_id: UUID | None = None,
        buying_role: str | None = None,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        return self._repos.contacts.list_all(
            conn,
            limit=limit,
            query=query,
            company_id=company_id,
            buying_role=buying_role,
            include_archived=include_archived,
        )

    def get_contact(
        self,
        conn: psycopg.Connection,
        contact_id: UUID,
    ) -> dict[str, Any] | None:
        return self._repos.contacts.get_by_id(conn, contact_id)

    def create_contact(
        self,
        conn: psycopg.Connection,
        *,
        contact: ContactCreate,
    ) -> dict[str, Any]:
        with crm_transaction(conn):
            profile_matches = (
                self._repos.contacts.find_by_profile_url(conn, contact.profile_url)
                if contact.profile_url
                else []
            )
            email_matches = (
                [existing]
                if contact.email and (existing := self._repos.contacts.get_by_email(conn, contact.email))
                else []
            )
            name_company_matches = (
                self._repos.contacts.find_by_name_company(
                    conn,
                    full_name=contact.full_name,
                    company_id=contact.company_id,
                )
                if contact.company_id
                else []
            )
            created = self._repos.contacts.create(conn, **contact.model_dump())
        duplicate_warnings = [
            *find_profile_url_duplicate_warnings(profile_matches, profile_url=contact.profile_url),
            *find_email_duplicate_warnings(email_matches, email=contact.email),
            *find_name_company_duplicate_warnings(
                name_company_matches,
                full_name=contact.full_name,
                company_id=contact.company_id,
            ),
        ]
        return {"contact": created, "duplicate_warnings": duplicate_warnings}

    def update_contact(
        self,
        conn: psycopg.Connection,
        contact_id: UUID,
        *,
        contact: ContactUpdate,
    ) -> dict[str, Any] | None:
        with crm_transaction(conn):
            profile_matches = (
                self._repos.contacts.find_by_profile_url(
                    conn, contact.profile_url, exclude_contact_id=contact_id
                )
                if contact.profile_url
                else []
            )
            email_matches: list[dict[str, Any]] = []
            if contact.email:
                existing = self._repos.contacts.get_by_email(conn, contact.email)
                if existing is not None and str(existing.get("id")) != str(contact_id):
                    email_matches.append(existing)
            name_company_matches = (
                self._repos.contacts.find_by_name_company(
                    conn,
                    full_name=contact.full_name,
                    company_id=contact.company_id,
                    exclude_contact_id=contact_id,
                )
                if contact.company_id
                else []
            )
            updated = self._repos.contacts.update(
                conn, contact_id, **contact.model_dump()
            )
        if updated is None:
            return None
        duplicate_warnings = [
            *find_profile_url_duplicate_warnings(
                profile_matches, profile_url=contact.profile_url, exclude_contact_id=contact_id
            ),
            *find_email_duplicate_warnings(
                email_matches, email=contact.email, exclude_contact_id=contact_id
            ),
            *find_name_company_duplicate_warnings(
                name_company_matches,
                full_name=contact.full_name,
                company_id=contact.company_id,
                exclude_contact_id=contact_id,
            ),
        ]
        return {"contact": updated, "duplicate_warnings": duplicate_warnings}

    def search_contacts(self, *args, **kwargs):
        """Compat alias for older call sites; prefer list_contacts."""
        return self.list_contacts(*args, **kwargs)

    def archive_contact(
        self, conn: psycopg.Connection, contact_id: UUID
    ) -> dict[str, Any] | None:
        with crm_transaction(conn):
            return self._repos.contacts.archive(conn, contact_id)

    def restore_contact(
        self, conn: psycopg.Connection, contact_id: UUID
    ) -> dict[str, Any] | None:
        with crm_transaction(conn):
            return self._repos.contacts.restore(conn, contact_id)

    def list_research_for_company(
        self,
        conn: psycopg.Connection,
        company_id: UUID,
        *,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return self._repos.research_records.list_for_company(
            conn,
            company_id,
            limit=limit,
        )

    def list_research_for_contact(
        self,
        conn: psycopg.Connection,
        contact_id: UUID,
        *,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return self._repos.research_records.list_for_contact(
            conn,
            contact_id,
            limit=limit,
        )

    def attach_research_record(
        self,
        conn: psycopg.Connection,
        *,
        record_type: str,
        company_id: UUID,
        body: str,
        contact_id: UUID | None = None,
        source_name: str | None = None,
        source_url: str | None = None,
        observed_value: str | None = None,
        observed_at: Any | None = None,
        confidence: float | None = None,
        review_at: Any | None = None,
        expires_at: Any | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Append a research record without overwriting prior observations."""
        with crm_transaction(conn):
            record = self._repos.research_records.create(
                conn,
                record_type=record_type,
                company_id=company_id,
                body=body,
                contact_id=contact_id,
                source_name=source_name,
                source_url=source_url,
                observed_value=observed_value,
                observed_at=observed_at,
                confidence=confidence,
                review_at=review_at,
                expires_at=expires_at,
                metadata=metadata,
            )
        return record

    def import_batch(
        self,
        conn: psycopg.Connection,
        *,
        actor_context: ActorContext,
        batch_id: str,
        source_type: str,
        records: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Import source records and append an audit event."""
        with crm_transaction(conn):
            created: list[dict[str, Any]] = []
            for index, record in enumerate(records):
                created.append(
                    self._repos.source_records.create(
                        conn,
                        source_type="import",
                        external_id=f"{batch_id}:{index}",
                        payload={"source_type": source_type, **record},
                    )
                )
            audit_service.record_import_batch(
                conn,
                actor_context=actor_context,
                batch_id=batch_id,
                source_type=source_type,
                record_count=len(created),
            )
        return {"batch_id": batch_id, "created": created, "record_count": len(created)}

    def delete_entity(
        self,
        conn: psycopg.Connection,
        *,
        actor_context: ActorContext,
        entity_type: str,
        entity_id: str,
        summary_before: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record a destructive delete audit event (storage delete ships later)."""
        with crm_transaction(conn):
            audit_service.record_entity_delete(
                conn,
                actor_context=actor_context,
                entity_type=entity_type,
                entity_id=entity_id,
                summary_before=summary_before,
            )
        return {"entity_type": entity_type, "entity_id": entity_id, "deleted": True}

    def update_pipeline(
        self,
        conn: psycopg.Connection,
        *,
        actor_context: ActorContext,
        entity_id: str,
        summary_before: dict[str, Any] | None,
        summary_after: dict[str, Any],
    ) -> dict[str, Any]:
        with crm_transaction(conn):
            audit_service.record_pipeline_update(
                conn,
                actor_context=actor_context,
                entity_id=entity_id,
                summary_before=summary_before,
                summary_after=summary_after,
            )
        return {"entity_id": entity_id, "summary_after": summary_after}

    def list_pipeline_companies(
        self,
        conn: psycopg.Connection,
        *,
        pipeline_stage: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return self._repos.pipeline.list_companies(
            conn, pipeline_stage=pipeline_stage, limit=limit
        )

    def get_pipeline_company(
        self,
        conn: psycopg.Connection,
        company_id: UUID,
    ) -> dict[str, Any] | None:
        return self._repos.pipeline.get_company_pipeline(conn, company_id)

    def list_pipeline_stage_history(
        self,
        conn: psycopg.Connection,
        company_id: UUID,
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        return self._repos.pipeline.list_stage_history(conn, company_id, limit=limit)

    def list_pipeline_overdue_actions(
        self,
        conn: psycopg.Connection,
        *,
        reference: datetime | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        ref = reference or datetime.now(timezone.utc)
        return self._repos.pipeline.list_overdue_next_actions(
            conn, reference=ref, limit=limit
        )

    def list_pipeline_upcoming_actions(
        self,
        conn: psycopg.Connection,
        *,
        reference: datetime | None = None,
        window_days: int = 14,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        ref = reference or datetime.now(timezone.utc)
        window_end = ref + timedelta(days=window_days)
        return self._repos.pipeline.list_upcoming_next_actions(
            conn, reference=ref, window_end=window_end, limit=limit
        )

    def transition_pipeline_stage(
        self,
        conn: psycopg.Connection,
        *,
        actor_context: ActorContext,
        company_id: UUID,
        change: PipelineStageChange,
    ) -> dict[str, Any]:
        company = self._repos.pipeline.get_company_pipeline(conn, company_id)
        if company is None:
            raise ValueError("Company not found.")
        from_stage = company.get("pipeline_stage")
        assess_stage_transition(
            from_stage,
            change.to_stage,
            confirm=change.confirm,
            loss_reason=change.loss_reason,
            nurture_reason=change.nurture_reason,
        )
        summary_before = pipeline_summary(company)
        loss_reason = change.loss_reason if change.to_stage == "lost" else None
        nurture_reason = change.nurture_reason if change.to_stage == "nurture" else None
        with crm_transaction(conn):
            updated = self._repos.pipeline.update_pipeline_fields(
                conn,
                company_id,
                pipeline_stage=change.to_stage,
                pipeline_loss_reason=loss_reason,
                pipeline_nurture_reason=nurture_reason,
                clear_loss_reason=change.to_stage != "lost",
                clear_nurture_reason=change.to_stage != "nurture",
            )
            if updated is None:
                raise ValueError("Company not found.")
            history = self._repos.pipeline.record_stage_history(
                conn,
                company_id=company_id,
                from_stage=from_stage,
                to_stage=change.to_stage,
                changed_by=actor_context.actor,
                metadata={
                    "confirm": change.confirm,
                    "loss_reason": change.loss_reason,
                    "nurture_reason": change.nurture_reason,
                },
            )
            self._repos.activities.create(
                conn,
                activity_type="status_change",
                summary=(
                    f"Pipeline stage: {from_stage or 'none'} → {change.to_stage}"
                ),
                company_id=company_id,
                metadata={
                    "from_stage": from_stage,
                    "to_stage": change.to_stage,
                    "history_id": str(history.get("id")),
                },
            )
            summary_after = pipeline_summary(updated)
            audit_service.record_pipeline_update(
                conn,
                actor_context=actor_context,
                entity_id=str(company_id),
                summary_before=summary_before,
                summary_after=summary_after,
            )
        return {"company": updated, "history": history}

    def update_pipeline_next_action(
        self,
        conn: psycopg.Connection,
        *,
        actor_context: ActorContext,
        company_id: UUID,
        update: PipelineNextActionUpdate,
    ) -> dict[str, Any]:
        company = self._repos.pipeline.get_company_pipeline(conn, company_id)
        if company is None:
            raise ValueError("Company not found.")
        summary_before = pipeline_summary(company)
        with crm_transaction(conn):
            updated = self._repos.pipeline.update_pipeline_fields(
                conn,
                company_id,
                next_action=update.next_action,
                next_action_due_at=update.next_action_due_at,
                pipeline_owner=update.pipeline_owner,
                expected_value_cents=update.expected_value_cents,
            )
            if updated is None:
                raise ValueError("Company not found.")
            summary_after = pipeline_summary(updated)
            audit_service.record_pipeline_update(
                conn,
                actor_context=actor_context,
                entity_id=str(company_id),
                summary_before=summary_before,
                summary_after=summary_after,
            )
        return {"company": updated}

    def record_pipeline_activity(
        self,
        conn: psycopg.Connection,
        *,
        company_id: UUID,
        activity: PipelineActivityCreate,
    ) -> dict[str, Any]:
        contact_id = UUID(activity.contact_id) if activity.contact_id else None
        with crm_transaction(conn):
            created = self._repos.activities.create(
                conn,
                activity_type=activity.activity_type,
                summary=activity.summary,
                company_id=company_id,
                contact_id=contact_id,
                metadata=activity.metadata,
            )
        return created

    def assign_company_to_pipeline(
        self,
        conn: psycopg.Connection,
        *,
        actor_context: ActorContext,
        company_id: UUID,
        initial_stage: str = "researching",
    ) -> dict[str, Any]:
        change = PipelineStageChange(to_stage=initial_stage)
        return self.transition_pipeline_stage(
            conn,
            actor_context=actor_context,
            company_id=company_id,
            change=change,
        )

    def update_scoring_rule(
        self,
        conn: psycopg.Connection,
        *,
        actor_context: ActorContext,
        rule_id: str,
        summary_before: dict[str, Any] | None,
        summary_after: dict[str, Any],
    ) -> dict[str, Any]:
        with crm_transaction(conn):
            audit_service.record_scoring_rule_update(
                conn,
                actor_context=actor_context,
                rule_id=rule_id,
                summary_before=summary_before,
                summary_after=summary_after,
            )
        return {"rule_id": rule_id, "summary_after": summary_after}

    def update_analytics_config(
        self,
        conn: psycopg.Connection,
        *,
        actor_context: ActorContext,
        config_key: str,
        summary_before: dict[str, Any] | None,
        summary_after: dict[str, Any],
    ) -> dict[str, Any]:
        with crm_transaction(conn):
            audit_service.record_analytics_config_update(
                conn,
                actor_context=actor_context,
                config_key=config_key,
                summary_before=summary_before,
                summary_after=summary_after,
            )
        return {"config_key": config_key, "summary_after": summary_after}

    def request_export(
        self,
        conn: psycopg.Connection,
        *,
        actor_context: ActorContext,
        export_type: str,
        filters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with crm_transaction(conn):
            audit_service.record_export_request(
                conn,
                actor_context=actor_context,
                export_type=export_type,
                filters=filters,
            )
        return {"export_type": export_type, "filters": filters or {}}

