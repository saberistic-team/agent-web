"""CRM service boundary — orchestrates repositories without embedding SQL."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import psycopg
from psycopg import errors as pg_errors

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
    ARCHIVED_CONTACT_ACK_REQUIRED_MESSAGE,
    BriefConversionError,
    BriefConversionIdempotencyRace,
    BriefConversionValidationError,
    build_conversion_proposal,
    safe_conversion_payload,
)
from app.brief_conversion_lock import acquire_brief_conversion_lock
from app.companies import (
    CompanyCreate,
    CompanyUpdate,
    find_domain_duplicate_warnings,
    normalize_domain,
)
from app.contacts import (
    ContactCreate,
    ContactEmailConflictError,
    ContactUpdate,
    find_email_duplicate_warnings,
    find_name_company_duplicate_warnings,
    find_profile_url_duplicate_warnings,
    ContactRestoreResult,
    ContactSafeSummary,
    contact_safe_summary,
    normalize_email,
)
from app.crm_lifecycle_audit import (
    company_transition_summary,
    contact_transition_summary,
    record_company_create,
    record_company_update_if_changed,
    record_contact_create,
    record_contact_update_if_changed,
)
from app.crm_uow import crm_transaction
from app.patch import UNSET
from app.linkedin_import import (
    LINKEDIN_IMPORT_SCHEMA_VERSION,
    SOURCE_KIND_CONNECTION,
    SOURCE_TYPE_LINKEDIN,
    compute_import_checksum,
    contact_matches_snapshot,
    empty_summary_counts,
    increment_summary,
    normalize_connection_row,
    parse_export_date,
    snapshot_contact,
)
from app.discovery.types import DiscoveryCandidate
from app.discovery_reconcile_ops import (
    DiscoveryReconcileOps,
    candidate_from_payload,
    candidates_from_payloads,
)
from app.linkedin_reconcile import (
    MatchResolution,
    compute_importable_updates,
    is_field_user_owned,
    parse_field_sources,
    preview_connection_row,
    resolve_company_id,
    resolve_connection_match,
)
from app.linkedin_relationship_metrics import (
    assert_no_message_bodies,
    build_connection_date_index,
    merge_relationship_metrics,
    metrics_last_interaction_date,
    strip_message_bodies,
)
from app.pipeline_stages import (
    initial_pipeline_stage_for_brief_status,
    pipeline_stage_label,
    validate_stage,
)
from app.icp_scoring import (
    IcpScoringRule,
    calculate_icp_score,
    default_icp_rules,
    rule_from_row,
)
from app.qualification_targets import (
    MAX_WORKING_LIST_ITEMS,
    QualificationTargetFilters,
    QualificationTargetRow,
    WorkingListCreate,
    build_target_row,
    filter_target_rows,
    rules_from_rows,
    score_company_with_rules,
    sort_target_rows,
    tier_change_metadata,
)
from app.repositories import (
    ActivityRepository,
    AdminUserRepository,
    CompanyRepository,
    ContactRepository,
    IcpScoringRepository,
    ImportBatchRepository,
    PipelineRepository,
    PostgresActivityRepository,
    PostgresAdminUserRepository,
    PostgresCompanyRepository,
    PostgresContactRepository,
    PostgresIcpScoringRepository,
    PostgresImportBatchRepository,
    PostgresPipelineRepository,
    PostgresQualificationRepository,
    PostgresResearchRecordRepository,
    PostgresSourceRecordRepository,
    QualificationRepository,
    ResearchRecordRepository,
    SourceRecordRepository,
)
from app.repositories.discovery_reconcile_postgres import (
    PostgresDiscoveryMergeDecisionRepository,
    PostgresDiscoveryReviewRepository,
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
    import_batches: ImportBatchRepository
    icp_scoring: IcpScoringRepository
    qualification: QualificationRepository
    discovery_review: Any = field(default_factory=PostgresDiscoveryReviewRepository)
    discovery_merge_decisions: Any = field(
        default_factory=PostgresDiscoveryMergeDecisionRepository
    )


def default_crm_repositories() -> CrmRepositories:
    return CrmRepositories(
        companies=PostgresCompanyRepository(),
        contacts=PostgresContactRepository(),
        source_records=PostgresSourceRecordRepository(),
        activities=PostgresActivityRepository(),
        research_records=PostgresResearchRecordRepository(),
        admin_users=PostgresAdminUserRepository(),
        pipeline=PostgresPipelineRepository(),
        import_batches=PostgresImportBatchRepository(),
        icp_scoring=PostgresIcpScoringRepository(),
        qualification=PostgresQualificationRepository(),
        discovery_review=PostgresDiscoveryReviewRepository(),
        discovery_merge_decisions=PostgresDiscoveryMergeDecisionRepository(),
    )



def _is_contact_email_unique_violation(exc: pg_errors.UniqueViolation) -> bool:
    diag = exc.diag
    if diag is None:
        return False
    return (diag.constraint_name or "") == "idx_contacts_email_unique"


_CONTACT_COMPANY_MISMATCH_MSG = (
    "Selected contact belongs to a different company — adjust your selection."
)
_CONTACT_STALE_MSG = "Selected contact is no longer active — refresh the preview and try again."
_CONTACT_EMAIL_MISMATCH_MSG = "Selected contact does not match the brief email."


class CrmService:
    """Thin service layer for admin/import/discovery callers."""

    def __init__(self, repos: CrmRepositories | None = None) -> None:
        self._repos = repos or default_crm_repositories()
        self._discovery_ops = DiscoveryReconcileOps(self._repos)

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
        # Active identity only drives linking. An archived-only match is surfaced
        # separately as a restore/review option and is never auto-linked (#226).
        active_contact = self._repos.contacts.get_active_by_email(conn, email)
        contacts = [active_contact] if active_contact else []
        archived_contact = (
            None if active_contact else self._repos.contacts.get_archived_by_email(conn, email)
        )
        return {
            "proposal": proposal,
            "company_matches": companies,
            "contact_matches": contacts,
            "archived_contact_match": archived_contact,
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
        acknowledge_archived_identity: bool = False,
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
        contact_match = self._repos.contacts.get_active_by_email(conn, email)
        archived_match = (
            None
            if contact_match
            else self._repos.contacts.get_archived_by_email(conn, email)
        )
        self._validate_conversion_choices(
            company_choice=company_choice,
            contact_choice=contact_choice,
            company_matches=company_matches,
            contact_match=contact_match,
            archived_match=archived_match,
            acknowledge_archived_identity=acknowledge_archived_identity,
            selected_company_id=selected_company_id,
            selected_contact_id=selected_contact_id,
        )

        try:
            return self._execute_brief_conversion(
                conn,
                brief=brief,
                brief_id=brief_id,
                actor_context=actor_context,
                proposal=proposal,
                pipeline_stage=pipeline_stage,
                domain=domain,
                email=email,
                company_choice=company_choice,
                contact_choice=contact_choice,
                selected_company_id=selected_company_id,
                selected_contact_id=selected_contact_id,
            )
        except BriefConversionIdempotencyRace:
            winner = self.get_project_brief_source(conn, brief_id)
            if winner is None:
                raise BriefConversionError(
                    "Brief conversion source link conflict but no winning record was found."
                ) from None
            return self._conversion_result_from_source(conn, winner, idempotent=True)

    def _execute_brief_conversion(
        self,
        conn: psycopg.Connection,
        *,
        brief: dict[str, Any],
        brief_id: int,
        actor_context: ActorContext,
        proposal: dict[str, Any],
        pipeline_stage: str,
        domain: str | None,
        email: str,
        company_choice: str,
        contact_choice: str,
        selected_company_id: UUID | None,
        selected_contact_id: UUID | None,
    ) -> dict[str, Any]:
        with crm_transaction(conn):
            acquire_brief_conversion_lock(conn, brief_id)

            race = self.get_project_brief_source(conn, brief_id)
            if race is not None:
                return self._conversion_result_from_source(conn, race, idempotent=True)

            locked_contact: dict[str, Any] | None = None
            if contact_choice == "existing":
                assert selected_contact_id is not None
                locked_contact = self._repos.contacts.get_active_by_id_for_update(
                    conn, selected_contact_id
                )
                if locked_contact is None:
                    raise BriefConversionValidationError("Selected contact was not found.")
                preview_target_company_id = (
                    selected_company_id if company_choice == "existing" else None
                )
                self._assert_contact_eligible_for_conversion(
                    contact=locked_contact,
                    email=email,
                    target_company_id=preview_target_company_id,
                )

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
                assert locked_contact is not None
                self._assert_contact_eligible_for_conversion(
                    contact=locked_contact,
                    email=email,
                    target_company_id=UUID(str(company["id"])),
                )
                contact = self._associate_contact_company(
                    conn, contact=locked_contact, company=company
                )
            else:
                contact = self._create_conversion_contact(
                    conn,
                    email=email,
                    proposal=proposal,
                    company=company,
                )

            updated = self._repos.pipeline.update_pipeline_fields(
                conn,
                UUID(str(company["id"])),
                pipeline_stage=pipeline_stage,
                # Omit rather than clear when the brief carries no expected value,
                # so linking to an existing company preserves its stored amount.
                expected_value_cents=(
                    expected_value_cents if expected_value_cents is not None else UNSET
                ),
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
            try:
                source_record = self._repos.source_records.create(
                    conn,
                    source_type="project_brief",
                    external_id=str(brief_id),
                    company_id=UUID(str(company["id"])),
                    contact_id=UUID(str(contact["id"])),
                    payload=payload,
                )
            except pg_errors.UniqueViolation as exc:
                if not self._is_brief_source_unique_violation(exc):
                    raise
                raise BriefConversionIdempotencyRace from exc

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

    @staticmethod
    def _is_brief_source_unique_violation(exc: pg_errors.UniqueViolation) -> bool:
        diag = exc.diag
        if diag is None:
            return False
        if diag.constraint_name == "source_records_type_external_unique":
            return True
        return diag.table_name == "source_records"

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

    def _associate_contact_company(
        self,
        conn: psycopg.Connection,
        *,
        contact: dict[str, Any],
        company: dict[str, Any],
    ) -> dict[str, Any]:
        """Apply the brief-conversion company-association rule (issue #226).

        When a brief supplies a company, linking an existing active contact only
        *fills in* a missing company association (``company_id IS NULL``). A
        contact that already belongs to a company keeps that association and is
        never silently reassigned; an explicit mismatch is rejected upstream in
        ``_validate_contact_company_association`` and
        ``_assert_contact_eligible_for_conversion``. See docs/CRM_SCHEMA.md.
        """
        company_id = company.get("id")
        if company_id is None or contact.get("company_id") is not None:
            return contact
        updated = self._repos.contacts.update(
            conn,
            UUID(str(contact["id"])),
            company_id=UUID(str(company_id)),
        )
        return updated or contact

    def _validate_contact_company_association(
        self,
        *,
        contact: dict[str, Any],
        target_company_id: UUID | None,
    ) -> None:
        """Reject an existing contact already assigned to a different company (#274)."""
        linked_company = contact.get("company_id")
        if linked_company is None:
            return
        if target_company_id is None:
            raise BriefConversionValidationError(_CONTACT_COMPANY_MISMATCH_MSG)
        if UUID(str(linked_company)) != target_company_id:
            raise BriefConversionValidationError(_CONTACT_COMPANY_MISMATCH_MSG)

    def _assert_contact_eligible_for_conversion(
        self,
        *,
        contact: dict[str, Any],
        email: str,
        target_company_id: UUID | None,
    ) -> None:
        """Revalidate active identity and company association inside the conversion txn."""
        if contact.get("archived_at") is not None:
            raise BriefConversionValidationError(_CONTACT_STALE_MSG)
        contact_email = str(contact.get("email") or "").strip().lower()
        if contact_email != email:
            raise BriefConversionValidationError(_CONTACT_EMAIL_MISMATCH_MSG)
        self._validate_contact_company_association(
            contact=contact,
            target_company_id=target_company_id,
        )

    def _create_conversion_contact(
        self,
        conn: psycopg.Connection,
        *,
        email: str,
        proposal: dict[str, Any],
        company: dict[str, Any],
    ) -> dict[str, Any]:
        """Create a contact for brief conversion; map email uniqueness races safely (#274)."""
        target_company_id = UUID(str(company["id"]))
        try:
            with conn.transaction():
                return self._repos.contacts.create(
                    conn,
                    email=email,
                    full_name=proposal.get("contact_name") or email.split("@", 1)[0],
                    company_id=target_company_id,
                )
        except pg_errors.UniqueViolation as exc:
            if not _is_contact_email_unique_violation(exc):
                raise
            existing = self._repos.contacts.get_active_by_email(conn, email)
            if existing is None:
                raise BriefConversionValidationError(
                    "A contact with this email already exists — link the existing contact."
                ) from exc
            self._assert_contact_eligible_for_conversion(
                contact=existing,
                email=email,
                target_company_id=target_company_id,
            )
            return self._associate_contact_company(
                conn, contact=existing, company=company
            )

    def _validate_conversion_choices(
        self,
        *,
        company_choice: str,
        contact_choice: str,
        company_matches: list[dict[str, Any]],
        contact_match: dict[str, Any] | None,
        archived_match: dict[str, Any] | None = None,
        acknowledge_archived_identity: bool = False,
        selected_company_id: UUID | None,
        selected_contact_id: UUID | None,
    ) -> None:
        if company_choice not in {"new", "existing"}:
            raise BriefConversionValidationError("Choose whether to create or link a company.")
        if contact_choice not in {"new", "existing"}:
            raise BriefConversionValidationError("Choose whether to create or link a contact.")

        if archived_match is not None and contact_choice == "new":
            if not acknowledge_archived_identity:
                raise BriefConversionValidationError(ARCHIVED_CONTACT_ACK_REQUIRED_MESSAGE)

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

        if contact_choice == "existing" and selected_contact_id is not None:
            contact = contact_match
            if contact is None:
                raise BriefConversionValidationError("Selected contact was not found.")
            preview_target_company_id = (
                selected_company_id if company_choice == "existing" else None
            )
            self._validate_contact_company_association(
                contact=contact,
                target_company_id=preview_target_company_id,
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
        actor_context: ActorContext,
    ) -> dict[str, Any]:
        with crm_transaction(conn):
            duplicates = self._repos.companies.find_by_domain(conn, company.domain) if company.domain else []
            created = self._repos.companies.create(conn, **company.model_dump())
            record_company_create(
                conn,
                actor_context=actor_context,
                company=created,
            )
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
        actor_context: ActorContext,
    ) -> dict[str, Any] | None:
        with crm_transaction(conn):
            existing = self._repos.companies.get_by_id(conn, company_id)
            if existing is None:
                return None
            duplicates = (
                self._repos.companies.find_by_domain(
                    conn, company.domain, exclude_company_id=company_id
                )
                if company.domain
                else []
            )
            updated = self._repos.companies.update(
                conn, company_id, **company.model_dump(exclude_unset=True)
            )
            if updated is None:
                return None
            record_company_update_if_changed(
                conn,
                actor_context=actor_context,
                entity_id=str(company_id),
                before_row=existing,
                after_row=updated,
            )
        return {
            "company": updated,
            "duplicate_warnings": find_domain_duplicate_warnings(
                duplicates, domain=company.domain, exclude_company_id=company_id
            ),
        }

    def archive_company(
        self,
        conn: psycopg.Connection,
        company_id: UUID,
        *,
        actor_context: ActorContext,
    ) -> dict[str, Any] | None:
        with crm_transaction(conn):
            existing = self._repos.companies.get_by_id(conn, company_id)
            if existing is None or existing.get("archived_at") is not None:
                return None
            summary_before = company_transition_summary(existing)
            archived = self._repos.companies.archive(conn, company_id)
            if archived is None:
                return None
            audit_service.record_company_archive(
                conn,
                actor_context=actor_context,
                entity_id=str(company_id),
                summary_before=summary_before,
                summary_after=company_transition_summary(archived),
            )
            return archived

    def restore_company(
        self,
        conn: psycopg.Connection,
        company_id: UUID,
        *,
        actor_context: ActorContext,
    ) -> dict[str, Any] | None:
        archived = self._repos.companies.get_by_id(conn, company_id)
        if archived is None or archived.get("archived_at") is None:
            return None
        with crm_transaction(conn):
            restored = self._repos.companies.restore(conn, company_id)
            if restored is None:
                return None
            audit_service.record_company_restore(
                conn,
                actor_context=actor_context,
                entity_id=str(company_id),
                summary_before=company_transition_summary(archived),
                summary_after=company_transition_summary(restored),
            )
            return restored

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
        actor_context: ActorContext,
    ) -> dict[str, Any]:
        with crm_transaction(conn):
            profile_matches = (
                self._repos.contacts.find_by_profile_url(conn, contact.profile_url)
                if contact.profile_url
                else []
            )
            email_matches = (
                [existing]
                if contact.email
                and (existing := self._repos.contacts.get_active_by_email(conn, contact.email))
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
            try:
                created = self._repos.contacts.create(conn, **contact.model_dump())
            except pg_errors.UniqueViolation as exc:
                if not _is_contact_email_unique_violation(exc):
                    raise
                raise ContactEmailConflictError(contact.email) from exc
            record_contact_create(
                conn,
                actor_context=actor_context,
                contact=created,
            )
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
        actor_context: ActorContext,
    ) -> dict[str, Any] | None:
        with crm_transaction(conn):
            existing = self._repos.contacts.get_by_id(conn, contact_id)
            if existing is None:
                return None
            profile_matches = (
                self._repos.contacts.find_by_profile_url(
                    conn, contact.profile_url, exclude_contact_id=contact_id
                )
                if contact.profile_url
                else []
            )
            email_matches: list[dict[str, Any]] = []
            if contact.email:
                existing_email = self._repos.contacts.get_active_by_email(
                    conn, contact.email, exclude_contact_id=contact_id
                )
                if existing_email is not None:
                    email_matches.append(existing_email)
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
            try:
                updated = self._repos.contacts.update(
                    conn, contact_id, **contact.model_dump(exclude_unset=True)
                )
            except pg_errors.UniqueViolation as exc:
                if not _is_contact_email_unique_violation(exc):
                    raise
                raise ContactEmailConflictError(contact.email) from exc
            if updated is None:
                return None
            record_contact_update_if_changed(
                conn,
                actor_context=actor_context,
                entity_id=str(contact_id),
                before_row=existing,
                after_row=updated,
            )
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
        self,
        conn: psycopg.Connection,
        contact_id: UUID,
        *,
        actor_context: ActorContext,
    ) -> dict[str, Any] | None:
        with crm_transaction(conn):
            existing = self._repos.contacts.get_by_id(conn, contact_id)
            if existing is None or existing.get("archived_at") is not None:
                return None
            summary_before = contact_transition_summary(existing)
            archived = self._repos.contacts.archive(conn, contact_id)
            if archived is None:
                return None
            audit_service.record_contact_archive(
                conn,
                actor_context=actor_context,
                entity_id=str(contact_id),
                summary_before=summary_before,
                summary_after=contact_transition_summary(archived),
            )
            return archived

    def restore_contact(
        self,
        conn: psycopg.Connection,
        contact_id: UUID,
        *,
        actor_context: ActorContext,
    ) -> ContactRestoreResult:
        archived = self._repos.contacts.get_by_id(conn, contact_id)
        if archived is None or archived.get("archived_at") is None:
            return ContactRestoreResult(outcome="not_found")

        conflicting = self._active_email_conflict_for_restore(
            conn,
            archived,
            exclude_contact_id=contact_id,
        )
        if conflicting is not None:
            return ContactRestoreResult(
                outcome="conflict",
                archived_contact=archived,
                conflicting_contact=conflicting,
            )

        try:
            with crm_transaction(conn):
                restored = self._repos.contacts.restore(conn, contact_id)
                if restored is None:
                    return ContactRestoreResult(outcome="not_found")
                audit_service.record_contact_restore(
                    conn,
                    actor_context=actor_context,
                    contact_id=str(contact_id),
                    summary_before=contact_transition_summary(archived),
                    summary_after=contact_transition_summary(restored),
                )
                return ContactRestoreResult(outcome="success", contact=restored)
        except pg_errors.UniqueViolation as exc:
            if not _is_contact_email_unique_violation(exc):
                raise
            conflicting = self._active_email_conflict_for_restore(
                conn,
                archived,
                exclude_contact_id=contact_id,
            )
            return ContactRestoreResult(
                outcome="conflict",
                archived_contact=archived,
                conflicting_contact=conflicting,
            )

    def get_contact_restore_conflict(
        self,
        conn: psycopg.Connection,
        contact_id: UUID,
    ) -> ContactRestoreResult | None:
        archived = self._repos.contacts.get_by_id(conn, contact_id)
        if archived is None or archived.get("archived_at") is None:
            return None
        conflicting = self._active_email_conflict_for_restore(
            conn,
            archived,
            exclude_contact_id=contact_id,
        )
        if conflicting is None:
            return None
        return ContactRestoreResult(
            outcome="conflict",
            archived_contact=archived,
            conflicting_contact=conflicting,
        )

    def _active_email_conflict_for_restore(
        self,
        conn: psycopg.Connection,
        archived_contact: dict[str, Any],
        *,
        exclude_contact_id: UUID,
    ) -> ContactSafeSummary | None:
        try:
            normalized = normalize_email(archived_contact.get("email"))
        except ValueError:
            return None
        if normalized is None:
            return None
        active = self._repos.contacts.get_active_by_email(
            conn,
            normalized,
            exclude_contact_id=exclude_contact_id,
        )
        if active is None:
            return None
        return contact_safe_summary(active, company_name=active.get("company_name"))

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
        actor_context: ActorContext,
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
            audit_service.record_research_record_create(
                conn,
                actor_context=actor_context,
                research_record_id=str(record["id"]),
                summary_after=audit_service.research_record_audit_summary(
                    research_record_id=str(record["id"]),
                    company_id=str(company_id),
                    contact_id=str(contact_id) if contact_id is not None else None,
                    record_type=record_type,
                    source_name=source_name,
                    source_url=source_url,
                    observed_value=observed_value,
                    observed_at=observed_at,
                    confidence=confidence,
                    review_at=review_at,
                    expires_at=expires_at,
                ),
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

    def _resolve_linkedin_match(
        self,
        conn: psycopg.Connection,
        identity: dict[str, Any],
    ) -> tuple[MatchResolution, UUID | None]:
        profile_url = identity.get("profile_url")
        profile_matches: list[dict[str, Any]] = []
        if profile_url:
            profile_matches = self._repos.contacts.find_by_profile_url(conn, profile_url)

        email_match: dict[str, Any] | None = None
        import_email = identity.get("email")
        if import_email:
            email_match = self._repos.contacts.get_active_by_email(conn, import_email)

        company_id: UUID | None = None
        company_ambiguity: list[dict[str, Any]] = []
        name_company_matches: list[dict[str, Any]] = []
        company_name = identity.get("company_name")
        if company_name:
            company_matches = self._repos.companies.find_by_exact_name(conn, company_name)
            company_id, company_ambiguity = resolve_company_id(
                company_name,
                companies_by_name={
                    company_name.strip().lower(): company_matches,
                },
            )
            if company_id is not None and identity.get("full_name"):
                name_company_matches = self._repos.contacts.find_by_name_company(
                    conn,
                    full_name=str(identity["full_name"]),
                    company_id=company_id,
                )

        match = resolve_connection_match(
            identity,
            profile_matches=profile_matches,
            email_match=email_match,
            name_company_matches=name_company_matches,
            company_ambiguity=company_ambiguity,
        )
        return match, company_id

    def preview_linkedin_reconcile(
        self,
        conn: psycopg.Connection,
        *,
        connections: list[dict[str, Any]],
        batch_id: str = "preview",
    ) -> dict[str, Any]:
        """Dry-run incremental reconciliation for a LinkedIn connections export."""
        existing_count = self._repos.contacts.count_active(conn)
        rows: list[dict[str, Any]] = []
        summary = {
            "insert": 0,
            "update": 0,
            "unchanged": 0,
            "conflict": 0,
            "skipped": 0,
        }

        for index, raw_row in enumerate(connections):
            identity = normalize_connection_row(raw_row)
            match, company_id = self._resolve_linkedin_match(conn, identity)
            preview_row = preview_connection_row(
                row_index=index,
                raw_row=raw_row,
                match=match,
                company_id=company_id,
                batch_id=batch_id,
            )
            rows.append(
                {
                    "row_index": preview_row.row_index,
                    "outcome": preview_row.outcome,
                    "identity": preview_row.identity,
                    "match_tier": preview_row.match_tier,
                    "contact_id": preview_row.contact_id,
                    "contact_label": preview_row.contact_label,
                    "field_changes": [
                        {"field": change.field, "before": change.before, "after": change.after}
                        for change in preview_row.field_changes
                    ],
                    "conflict_reason": preview_row.conflict_reason,
                    "conflict_candidates": [
                        {
                            "contact_id": candidate.contact_id,
                            "full_name": candidate.full_name,
                            "title": candidate.title,
                            "company_name": candidate.company_name,
                            "profile_url": candidate.profile_url,
                            "email": candidate.email,
                        }
                        for candidate in preview_row.conflict_candidates
                    ],
                    "detail": preview_row.detail,
                }
            )
            summary[preview_row.outcome] += 1

        touched_ids = {
            row["contact_id"]
            for row in rows
            if row.get("contact_id") and row["outcome"] in {"update", "unchanged"}
        }
        return {
            "rows": rows,
            "summary_counts": summary,
            "absent_preserved": max(existing_count - len(touched_ids), 0),
        }

    def commit_linkedin_import(
        self,
        conn: psycopg.Connection,
        *,
        actor_context: ActorContext,
        connections: list[dict[str, Any]],
        export_date: Any | None = None,
        checksum: str | None = None,
        message_metadata: list[dict[str, Any]] | None = None,
        owner_name: str | None = None,
    ) -> dict[str, Any]:
        """Merge LinkedIn connections incrementally without deleting absent records."""
        resolved_checksum = checksum or compute_import_checksum(connections)
        existing = self._repos.import_batches.get_committed_by_checksum(conn, resolved_checksum)
        if existing is not None:
            rows = self._repos.import_batches.list_rows_for_batch(
                conn, UUID(str(existing["id"]))
            )
            return {
                "batch": existing,
                "rows": rows,
                "idempotent": True,
                "summary_counts": existing.get("summary_counts") or {},
            }

        summary = empty_summary_counts()
        row_records: list[dict[str, Any]] = []
        seen_at = datetime.now(timezone.utc)

        with crm_transaction(conn):
            batch = self._repos.import_batches.create(
                conn,
                source_type=SOURCE_TYPE_LINKEDIN,
                schema_version=LINKEDIN_IMPORT_SCHEMA_VERSION,
                checksum=resolved_checksum,
                actor=actor_context.actor,
                status="committed",
                correlation_id=actor_context.correlation_id,
                export_date=parse_export_date(export_date),
                summary_counts=summary,
            )
            batch_uuid = UUID(str(batch["id"]))
            batch_id_str = str(batch_uuid)

            for index, raw_row in enumerate(connections):
                identity = normalize_connection_row(raw_row)
                match, company_id = self._resolve_linkedin_match(conn, identity)
                preview_row = preview_connection_row(
                    row_index=index,
                    raw_row=raw_row,
                    match=match,
                    company_id=company_id,
                    batch_id=batch_id_str,
                    seen_at=seen_at,
                )

                if preview_row.outcome == "skipped":
                    increment_summary(summary, "skipped")
                    row_records.append(
                        {
                            "row_index": index,
                            "source_kind": SOURCE_KIND_CONNECTION,
                            "source_identity": identity,
                            "outcome": "skipped",
                            "detail": preview_row.detail,
                        }
                    )
                    continue

                if preview_row.outcome == "conflict":
                    increment_summary(summary, "conflicted")
                    row_records.append(
                        {
                            "row_index": index,
                            "source_kind": SOURCE_KIND_CONNECTION,
                            "source_identity": identity,
                            "outcome": "conflicted",
                            "detail": preview_row.conflict_reason,
                        }
                    )
                    continue

                if preview_row.outcome == "insert":
                    updates, field_sources, _ = compute_importable_updates(
                        None,
                        identity,
                        company_id=company_id,
                        batch_id=batch_id_str,
                        seen_at=seen_at,
                    )
                    contact = self._repos.contacts.create(
                        conn,
                        full_name=updates.get("full_name")
                        or identity.get("full_name")
                        or str(identity.get("profile_url") or "Unknown"),
                        title=updates.get("title"),
                        profile_url=updates.get("profile_url"),
                        email=updates.get("email"),
                        email_permission=updates.get("email_permission"),
                        company_id=updates.get("company_id"),
                        last_interaction_at=updates.get("last_interaction_at"),
                        field_sources=field_sources,
                    )
                    applied = snapshot_contact(contact)
                    increment_summary(summary, "inserted")
                    row_records.append(
                        {
                            "row_index": index,
                            "source_kind": SOURCE_KIND_CONNECTION,
                            "source_identity": identity,
                            "outcome": "inserted",
                            "entity_type": "contact",
                            "entity_id": UUID(str(contact["id"])),
                            "prior_snapshot": None,
                            "applied_snapshot": applied,
                        }
                    )
                    self._repos.source_records.create(
                        conn,
                        source_type="import",
                        external_id=f"{batch_uuid}:{index}",
                        contact_id=UUID(str(contact["id"])),
                        payload={"source_kind": SOURCE_KIND_CONNECTION, **identity},
                    )
                    continue

                contact = match.contact
                if contact is None:
                    increment_summary(summary, "skipped")
                    row_records.append(
                        {
                            "row_index": index,
                            "source_kind": SOURCE_KIND_CONNECTION,
                            "source_identity": identity,
                            "outcome": "skipped",
                            "detail": "Match resolution missing contact",
                        }
                    )
                    continue

                if preview_row.outcome == "unchanged":
                    increment_summary(summary, "unchanged")
                    row_records.append(
                        {
                            "row_index": index,
                            "source_kind": SOURCE_KIND_CONNECTION,
                            "source_identity": identity,
                            "outcome": "unchanged",
                            "entity_type": "contact",
                            "entity_id": UUID(str(contact["id"])),
                            "prior_snapshot": snapshot_contact(contact),
                            "applied_snapshot": snapshot_contact(contact),
                        }
                    )
                    continue

                prior = snapshot_contact(contact)
                updates, field_sources, _ = compute_importable_updates(
                    contact,
                    identity,
                    company_id=company_id,
                    batch_id=batch_id_str,
                    seen_at=seen_at,
                )
                updated = self._repos.contacts.update(
                    conn,
                    UUID(str(contact["id"])),
                    full_name=updates.get("full_name"),
                    title=updates.get("title"),
                    profile_url=updates.get("profile_url"),
                    email=updates.get("email"),
                    email_permission=updates.get("email_permission"),
                    company_id=updates.get("company_id"),
                    last_interaction_at=updates.get("last_interaction_at"),
                    field_sources=field_sources,
                )
                if updated is None:
                    increment_summary(summary, "conflicted")
                    row_records.append(
                        {
                            "row_index": index,
                            "source_kind": SOURCE_KIND_CONNECTION,
                            "source_identity": identity,
                            "outcome": "conflicted",
                            "entity_type": "contact",
                            "entity_id": UUID(str(contact["id"])),
                            "prior_snapshot": prior,
                            "detail": "Contact update failed",
                        }
                    )
                    continue

                applied = snapshot_contact(updated)
                increment_summary(summary, "updated")
                row_records.append(
                    {
                        "row_index": index,
                        "source_kind": SOURCE_KIND_CONNECTION,
                        "source_identity": identity,
                        "outcome": "updated",
                        "entity_type": "contact",
                        "entity_id": UUID(str(updated["id"])),
                        "prior_snapshot": prior,
                        "applied_snapshot": applied,
                    }
                )
                self._repos.source_records.create(
                    conn,
                    source_type="import",
                    external_id=f"{batch_uuid}:{index}",
                    contact_id=UUID(str(updated["id"])),
                    payload={"source_kind": SOURCE_KIND_CONNECTION, **identity},
                )

            if message_metadata and owner_name:
                self._apply_linkedin_relationship_metrics(
                    conn,
                    connections=connections,
                    message_rows=message_metadata,
                    owner_name=owner_name,
                    export_date=export_date,
                    touched_contact_ids={
                        UUID(str(record["entity_id"]))
                        for record in row_records
                        if record.get("entity_id") is not None
                    },
                    seen_at=seen_at,
                )

            persisted_rows: list[dict[str, Any]] = []
            for record in row_records:
                persisted_rows.append(
                    self._repos.import_batches.create_row(
                        conn,
                        batch_id=batch_uuid,
                        row_index=record["row_index"],
                        source_kind=record["source_kind"],
                        source_identity=record["source_identity"],
                        outcome=record["outcome"],
                        entity_type=record.get("entity_type"),
                        entity_id=record.get("entity_id"),
                        prior_snapshot=record.get("prior_snapshot"),
                        applied_snapshot=record.get("applied_snapshot"),
                        detail=record.get("detail"),
                    )
                )

            batch = self._repos.import_batches.update_status(
                conn,
                batch_uuid,
                status="committed",
                summary_counts=summary,
            ) or batch

            audit_service.record_import_batch(
                conn,
                actor_context=actor_context,
                batch_id=str(batch_uuid),
                source_type=SOURCE_TYPE_LINKEDIN,
                record_count=len(persisted_rows),
                schema_version=LINKEDIN_IMPORT_SCHEMA_VERSION,
                checksum=resolved_checksum,
                export_date=parse_export_date(export_date),
                summary_counts=summary,
            )

        return {
            "batch": batch,
            "rows": persisted_rows,
            "idempotent": False,
            "summary_counts": summary,
        }

    def _apply_linkedin_relationship_metrics(
        self,
        conn: psycopg.Connection,
        *,
        connections: list[dict[str, Any]],
        message_rows: list[dict[str, Any]],
        owner_name: str,
        export_date: Any | None,
        touched_contact_ids: set[UUID],
        seen_at: datetime,
    ) -> None:
        assert_no_message_bodies(message_rows)
        safe_rows = strip_message_bodies(message_rows)
        reference_date = parse_export_date(export_date) or seen_at.date()
        connection_index = build_connection_date_index(connections)

        for contact_id in touched_contact_ids:
            contact = self._repos.contacts.get_by_id(conn, contact_id)
            if contact is None or contact.get("archived_at") is not None:
                continue
            contact_name = str(contact.get("full_name") or "")
            if not contact_name.strip():
                continue
            profile_url = contact.get("profile_url")
            connection_date = connection_index.get(str(profile_url)) if profile_url else None
            existing_metrics = contact.get("relationship_metrics")
            if not isinstance(existing_metrics, dict):
                existing_metrics = {}
            merged = merge_relationship_metrics(
                existing_metrics,
                contact_name=contact_name,
                owner_name=owner_name,
                connection_date=connection_date,
                message_rows=safe_rows,
                reference_date=reference_date,
            )
            updates: dict[str, Any] = {"relationship_metrics": merged}
            field_sources = parse_field_sources(contact.get("field_sources"))
            metrics_last = metrics_last_interaction_date(merged)
            if metrics_last is not None and not is_field_user_owned(field_sources, "last_interaction_at"):
                current_last = contact.get("last_interaction_at")
                if current_last is None or metrics_last > current_last:
                    updates["last_interaction_at"] = metrics_last
            self._repos.contacts.update(conn, contact_id, **updates)

    def get_import_batch(
        self,
        conn: psycopg.Connection,
        batch_id: UUID,
    ) -> dict[str, Any] | None:
        batch = self._repos.import_batches.get_by_id(conn, batch_id)
        if batch is None:
            return None
        rows = self._repos.import_batches.list_rows_for_batch(conn, batch_id)
        return {"batch": batch, "rows": rows}

    def list_import_batches(
        self,
        conn: psycopg.Connection,
        *,
        page: int = 1,
        per_page: int = 50,
    ) -> tuple[list[dict[str, Any]], int]:
        return self._repos.import_batches.list_page(conn, page=page, per_page=per_page)

    def rollback_import_batch(
        self,
        conn: psycopg.Connection,
        *,
        actor_context: ActorContext,
        batch_id: UUID,
    ) -> dict[str, Any]:
        """Reverse batch-owned contact changes without clobbering later edits."""
        state = self.get_import_batch(conn, batch_id)
        if state is None:
            raise ValueError("Import batch was not found.")
        batch = state["batch"]
        if batch.get("status") != "committed":
            raise ValueError("Only committed import batches can be rolled back.")

        rollback_summary = {
            "reverted_inserts": 0,
            "reverted_updates": 0,
            "skipped_later_edits": 0,
            "skipped_non_reversible": 0,
        }

        with crm_transaction(conn):
            for row in state["rows"]:
                outcome = row.get("outcome")
                entity_id = row.get("entity_id")
                if entity_id is None or outcome not in {"inserted", "updated"}:
                    rollback_summary["skipped_non_reversible"] += 1
                    continue

                contact = self._repos.contacts.get_by_id(conn, UUID(str(entity_id)))
                if contact is None:
                    rollback_summary["skipped_non_reversible"] += 1
                    continue

                applied = row.get("applied_snapshot")
                prior = row.get("prior_snapshot")
                if not contact_matches_snapshot(contact, applied):
                    rollback_summary["skipped_later_edits"] += 1
                    continue

                if outcome == "inserted":
                    archived = self._repos.contacts.archive(conn, UUID(str(entity_id)))
                    if archived is not None:
                        rollback_summary["reverted_inserts"] += 1
                    else:
                        rollback_summary["skipped_non_reversible"] += 1
                elif outcome == "updated" and prior is not None:
                    restored = self._repos.contacts.update(
                        conn,
                        UUID(str(entity_id)),
                        full_name=prior.get("full_name"),
                        title=prior.get("title"),
                        profile_url=prior.get("profile_url"),
                        email=prior.get("email"),
                        company_id=UUID(str(prior["company_id"]))
                        if prior.get("company_id")
                        else None,
                        field_sources=prior.get("field_sources"),
                    )
                    if restored is not None:
                        rollback_summary["reverted_updates"] += 1
                    else:
                        rollback_summary["skipped_non_reversible"] += 1

            updated_batch = self._repos.import_batches.update_status(
                conn,
                batch_id,
                status="rolled_back",
            )
            audit_service.record_import_batch_rollback(
                conn,
                actor_context=actor_context,
                batch_id=str(batch_id),
                summary_before={"status": "committed", "summary_counts": batch.get("summary_counts")},
                summary_after={
                    "status": "rolled_back",
                    "rollback_summary": rollback_summary,
                },
            )

        return {
            "batch": updated_batch,
            "rollback_summary": rollback_summary,
        }

    def list_import_conflicts(
        self,
        conn: psycopg.Connection,
        *,
        batch_id: UUID | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return unresolved conflict rows from import batches."""
        if batch_id is not None:
            return self._repos.import_batches.list_rows_for_batch(
                conn,
                batch_id,
                outcome="conflicted",
                limit=limit,
            )
        batches, _ = self._repos.import_batches.list_page(conn, page=1, per_page=20)
        conflicts: list[dict[str, Any]] = []
        for batch in batches:
            rows = self._repos.import_batches.list_rows_for_batch(
                conn,
                UUID(str(batch["id"])),
                outcome="conflicted",
                limit=limit,
            )
            conflicts.extend(rows)
            if len(conflicts) >= limit:
                break
        return conflicts[:limit]


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
        to_lost = change.to_stage == "lost"
        to_nurture = change.to_stage == "nurture"
        # Set the reason only when a real value is supplied; otherwise omit it and
        # let the clear_* flags reset the reason that no longer applies. Passing a
        # value and the matching clear flag together would double-assign the column.
        loss_reason = change.loss_reason if (to_lost and change.loss_reason) else UNSET
        nurture_reason = (
            change.nurture_reason if (to_nurture and change.nurture_reason) else UNSET
        )
        with crm_transaction(conn):
            updated = self._repos.pipeline.update_pipeline_fields(
                conn,
                company_id,
                pipeline_stage=change.to_stage,
                pipeline_loss_reason=loss_reason,
                pipeline_nurture_reason=nurture_reason,
                clear_loss_reason=not to_lost,
                clear_nurture_reason=not to_nurture,
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
        # Only fields the caller actually supplied are patched; a supplied blank
        # (mapped to None by the model) clears the column, while omitted fields
        # keep their stored value.
        patch = update.model_dump(exclude_unset=True)
        with crm_transaction(conn):
            updated = self._repos.pipeline.update_pipeline_fields(
                conn,
                company_id,
                **patch,
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
        actor_context: ActorContext,
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
            audit_service.record_pipeline_activity_create(
                conn,
                actor_context=actor_context,
                activity_id=str(created["id"]),
                summary_after=audit_service.pipeline_activity_audit_summary(
                    activity_id=str(created["id"]),
                    company_id=str(company_id),
                    contact_id=str(contact_id) if contact_id is not None else None,
                    activity_type=activity.activity_type,
                    created_at=created.get("created_at"),
                ),
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

    def list_active_icp_rules(self, conn: psycopg.Connection) -> list[dict[str, Any]]:
        version = self._repos.icp_scoring.get_active_version(conn)
        if version is None:
            return []
        return self._repos.icp_scoring.list_rules_for_version(conn, version["id"])

    def get_active_icp_version(self, conn: psycopg.Connection) -> dict[str, Any] | None:
        return self._repos.icp_scoring.get_active_version(conn)

    def publish_icp_rule_version(
        self,
        conn: psycopg.Connection,
        *,
        actor_context: ActorContext,
        rules: list[IcpScoringRule],
        label: str | None = None,
    ) -> dict[str, Any]:
        active = self._repos.icp_scoring.get_active_version(conn)
        if active is None:
            raise ValueError("No active ICP scoring version configured.")
        current_rules = [
            rule_from_row(row)
            for row in self._repos.icp_scoring.list_rules_for_version(conn, active["id"])
        ]
        current_by_id = {rule.id: rule for rule in current_rules}
        next_number = int(active["version_number"]) + 1
        version_label = label or f"ICP rules v{next_number}"

        with crm_transaction(conn):
            self._repos.icp_scoring.deactivate_all_versions(conn)
            created_version = self._repos.icp_scoring.create_version(
                conn,
                version_number=next_number,
                label=version_label,
                created_by=actor_context.actor,
                activate=True,
            )
            stored_rules: list[dict[str, Any]] = []
            for rule in sorted(rules, key=lambda item: (item.sort_order, item.id)):
                stored = self._repos.icp_scoring.insert_rule(
                    conn,
                    version_id=created_version["id"],
                    rule_id=rule.id,
                    dimension=rule.dimension,
                    label=rule.label,
                    weight=rule.weight,
                    threshold=rule.threshold.model_dump(),
                    enabled=rule.enabled,
                    accept_hypothesis=rule.accept_hypothesis,
                    sort_order=rule.sort_order,
                )
                stored_rules.append(stored)
                prior = current_by_id.get(rule.id)
                if prior is None or prior.model_dump() != rule.model_dump():
                    audit_service.record_scoring_rule_update(
                        conn,
                        actor_context=actor_context,
                        rule_id=rule.id,
                        summary_before=prior.model_dump() if prior else None,
                        summary_after=rule.model_dump(),
                    )
        return {"version": created_version, "rules": stored_rules}

    def calculate_company_icp_score(
        self,
        conn: psycopg.Connection,
        *,
        actor_context: ActorContext,
        company_id: UUID,
        calculated_at: datetime | None = None,
    ) -> dict[str, Any]:
        company = self._repos.companies.get_by_id(conn, company_id)
        if company is None:
            raise ValueError("Company not found.")
        version = self._repos.icp_scoring.get_active_version(conn)
        if version is None:
            raise ValueError("No active ICP scoring version configured.")
        rules = [
            rule_from_row(row)
            for row in self._repos.icp_scoring.list_rules_for_version(conn, version["id"])
        ]
        contacts = self._repos.contacts.list_for_company(conn, company_id)
        research_records = self._repos.research_records.list_for_company(conn, company_id)
        result = calculate_icp_score(
            company=company,
            contacts=contacts,
            research_records=research_records,
            rules=rules,
            version_number=int(version["version_number"]),
            calculated_at=calculated_at,
        )
        with crm_transaction(conn):
            snapshot = self._repos.icp_scoring.insert_snapshot(
                conn,
                company_id=company_id,
                version_id=version["id"],
                version_number=result.version_number,
                total_score=result.total_score,
                computed_score=result.computed_score,
                breakdown=[item.model_dump() for item in result.breakdown],
                missing_inputs=result.missing_inputs,
                calculated_at=result.calculated_at,
            )
        return {"company": company, "result": result, "snapshot": snapshot}

    def override_company_icp_score(
        self,
        conn: psycopg.Connection,
        *,
        actor_context: ActorContext,
        company_id: UUID,
        override_score: float,
        reason: str,
        calculated_at: datetime | None = None,
    ) -> dict[str, Any]:
        stripped_reason = reason.strip()
        if not stripped_reason:
            raise ValueError("Override reason is required.")
        if override_score < 0 or override_score > 10:
            raise ValueError("Override score must be between 0 and 10.")
        company = self._repos.companies.get_by_id(conn, company_id)
        if company is None:
            raise ValueError("Company not found.")
        version = self._repos.icp_scoring.get_active_version(conn)
        if version is None:
            raise ValueError("No active ICP scoring version configured.")
        rules = [
            rule_from_row(row)
            for row in self._repos.icp_scoring.list_rules_for_version(conn, version["id"])
        ]
        contacts = self._repos.contacts.list_for_company(conn, company_id)
        research_records = self._repos.research_records.list_for_company(conn, company_id)
        result = calculate_icp_score(
            company=company,
            contacts=contacts,
            research_records=research_records,
            rules=rules,
            version_number=int(version["version_number"]),
            calculated_at=calculated_at,
            is_override=True,
            override_reason=stripped_reason,
            override_by=actor_context.actor,
            override_score=override_score,
        )
        with crm_transaction(conn):
            snapshot = self._repos.icp_scoring.insert_snapshot(
                conn,
                company_id=company_id,
                version_id=version["id"],
                version_number=result.version_number,
                total_score=result.total_score,
                computed_score=result.computed_score,
                breakdown=[item.model_dump() for item in result.breakdown],
                missing_inputs=result.missing_inputs,
                calculated_at=result.calculated_at,
                is_override=True,
                override_reason=stripped_reason,
                override_by=actor_context.actor,
            )
        return {"company": company, "result": result, "snapshot": snapshot}

    def get_company_icp_score_detail(
        self,
        conn: psycopg.Connection,
        company_id: UUID,
    ) -> dict[str, Any] | None:
        company = self._repos.companies.get_by_id(conn, company_id)
        if company is None:
            return None
        snapshot = self._repos.icp_scoring.get_latest_snapshot_for_company(conn, company_id)
        version = self._repos.icp_scoring.get_active_version(conn)
        rules = []
        if version is not None:
            rules = self._repos.icp_scoring.list_rules_for_version(conn, version["id"])
        return {
            "company": company,
            "snapshot": snapshot,
            "active_version": version,
            "active_rules": rules,
        }

    def list_company_icp_scores(
        self,
        conn: psycopg.Connection,
        *,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return self._repos.icp_scoring.list_latest_snapshots(conn, limit=limit)

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

    @staticmethod
    def _target_row_to_dict(row: QualificationTargetRow) -> dict[str, Any]:
        return {
            "company_id": row.company_id,
            "id": row.company_id,
            "name": row.name,
            "score": row.score,
            "tier": row.tier,
            "stage": row.stage,
            "vertical": row.vertical,
            "strongest_signals": list(row.strongest_signals),
            "warm_path": row.warm_path,
            "has_warm_path": row.has_warm_path,
            "next_action": row.next_action,
            "evidence_freshness": row.evidence_freshness,
            "missing_fields": list(row.missing_fields),
            "pipeline_stage": row.pipeline_stage,
            "pipeline_owner": row.pipeline_owner,
            "score_calculated_at": row.score_calculated_at,
            "stale_evidence": row.stale_evidence,
        }

    def list_qualification_targets(
        self,
        conn: psycopg.Connection,
        *,
        filters: QualificationTargetFilters | None = None,
        actor: str | None = None,
        persist_scores: bool = True,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Build tier A/B/C target rows from deterministic ICP scores."""
        active_version = self._repos.icp_scoring.get_active_version(conn)
        if active_version is None:
            return []
        version_id = UUID(str(active_version["id"]))
        version_number = int(active_version["version_number"])
        rules = rules_from_rows(
            self._repos.icp_scoring.list_rules_for_version(conn, version_id)
        )
        companies = self._repos.qualification.list_active_companies(conn, limit=limit)
        rows: list[QualificationTargetRow] = []
        with crm_transaction(conn):
            for company in companies:
                company_id = UUID(str(company["id"]))
                contacts = self._repos.contacts.list_for_company(conn, company_id, limit=200)
                research = self._repos.research_records.list_for_company(
                    conn, company_id, limit=200
                )
                score_result = score_company_with_rules(
                    company=company,
                    contacts=contacts,
                    research_records=research,
                    rules=rules,
                    version_number=version_number,
                )
                target_row = build_target_row(company=company, score_result=score_result)
                if target_row is None:
                    continue
                if persist_scores and actor:
                    snapshot = self._repos.icp_scoring.insert_snapshot(
                        conn,
                        company_id=company_id,
                        version_id=version_id,
                        version_number=version_number,
                        total_score=score_result.total_score,
                        computed_score=score_result.computed_score,
                        breakdown=[item.model_dump() for item in score_result.breakdown],
                        missing_inputs=score_result.missing_inputs,
                        calculated_at=score_result.calculated_at,
                        is_override=score_result.is_override,
                        override_reason=score_result.override_reason,
                        override_by=score_result.override_by,
                    )
                    new_tier = target_row.tier
                    previous_tier = self._repos.qualification.get_latest_tier_for_company(
                        conn, company_id
                    )
                    if previous_tier != new_tier:
                        self._repos.qualification.record_tier_change(
                            conn,
                            company_id=company_id,
                            from_tier=previous_tier,
                            to_tier=new_tier,
                            score=score_result.total_score,
                            changed_by=actor,
                            snapshot_id=UUID(str(snapshot["id"])),
                            metadata=tier_change_metadata(
                                previous_tier=previous_tier,
                                new_tier=new_tier,
                                score=score_result.total_score,
                            ),
                        )
                rows.append(target_row)
        sorted_rows = sort_target_rows(rows)
        if filters:
            sorted_rows = filter_target_rows(sorted_rows, filters)
        return [self._target_row_to_dict(row) for row in sorted_rows]

    def list_qualification_tier_history(
        self,
        conn: psycopg.Connection,
        company_id: UUID,
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        return self._repos.qualification.list_tier_history(conn, company_id, limit=limit)

    def save_qualification_working_list(
        self,
        conn: psycopg.Connection,
        *,
        owner: str,
        payload: WorkingListCreate,
    ) -> dict[str, Any]:
        company_ids = [UUID(value) for value in payload.company_ids]
        if len(company_ids) > MAX_WORKING_LIST_ITEMS:
            raise ValueError(f"working list cannot exceed {MAX_WORKING_LIST_ITEMS} companies")
        with crm_transaction(conn):
            return self._repos.qualification.create_working_list(
                conn,
                name=payload.name.strip(),
                owner=owner,
                company_ids=company_ids,
                max_items=MAX_WORKING_LIST_ITEMS,
            )

    def list_qualification_working_lists(
        self,
        conn: psycopg.Connection,
        *,
        owner: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        return self._repos.qualification.list_working_lists_for_owner(
            conn, owner=owner, limit=limit
        )

    def get_qualification_working_list_items(
        self,
        conn: psycopg.Connection,
        list_id: UUID,
    ) -> list[dict[str, Any]]:
        return self._repos.qualification.get_working_list_items(conn, list_id)

    def preview_discovery_reconcile(
        self,
        conn: psycopg.Connection,
        *,
        candidates: list[dict[str, Any]] | list[Any],
        run_id: str = "preview",
    ) -> dict[str, Any]:
        """Dry-run reconciliation for normalized discovery candidates."""
        normalized = (
            candidates
            if candidates and isinstance(candidates[0], DiscoveryCandidate)
            else candidates_from_payloads(candidates)  # type: ignore[arg-type]
        )
        return self._discovery_ops.preview(conn, candidates=normalized, run_id=run_id)

    def commit_discovery_reconcile(
        self,
        conn: psycopg.Connection,
        *,
        actor_context: ActorContext,
        candidates: list[dict[str, Any]] | list[Any],
        run_id: str,
        merge_decisions: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Apply discovery reconciliation without deleting absent CRM companies."""
        normalized = (
            candidates
            if candidates and isinstance(candidates[0], DiscoveryCandidate)
            else candidates_from_payloads(candidates)  # type: ignore[arg-type]
        )
        return self._discovery_ops.commit(
            conn,
            actor_context=actor_context,
            candidates=normalized,
            run_id=run_id,
            merge_decisions=merge_decisions,
        )

    def record_discovery_merge_decision(
        self,
        conn: psycopg.Connection,
        *,
        actor_context: ActorContext,
        external_id: str,
        source_id: str,
        decision: str,
        company_id: str | UUID | None,
        candidate_payload: dict[str, Any],
        match_tier: str,
        notes: str | None = None,
    ) -> dict[str, Any]:
        """Persist an auditable operator merge decision for later runs."""
        candidate = candidate_from_payload(candidate_payload)
        return self._discovery_ops.record_merge_decision(
            conn,
            actor_context=actor_context,
            external_id=external_id,
            source_id=source_id,
            decision=decision,
            company_id=company_id,
            candidate=candidate,
            match_tier=match_tier,
            notes=notes,
        )

    def list_discovery_review_queue(
        self,
        conn: psycopg.Connection,
        *,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return self._discovery_ops.list_review_queue(conn, limit=limit)

