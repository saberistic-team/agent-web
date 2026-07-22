"""Business logic for lead discovery inbox review actions."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import psycopg

from app import audit_service
from app.actor_context import ActorContext
from app.crm_service import CrmRepositories, default_crm_repositories
from app.crm_uow import crm_transaction
from app.discovery.category import crm_category_for_discovery
from app.discovery_inbox import (
    DISCOVERY_BULK_MAX,
    BulkAction,
    DiscoveryBulkLimitError,
    DiscoveryCandidateNotFoundError,
    DiscoveryCandidateStateError,
    DiscoveryInboxError,
    DiscoveryInboxFilters,
)
from app.repositories.discovery_inbox_postgres import PostgresDiscoveryInboxRepository


@dataclass(frozen=True)
class DiscoveryInboxRepositories:
    inbox: PostgresDiscoveryInboxRepository
    crm: CrmRepositories


def default_discovery_inbox_repositories() -> DiscoveryInboxRepositories:
    return DiscoveryInboxRepositories(
        inbox=PostgresDiscoveryInboxRepository(),
        crm=default_crm_repositories(),
    )


class DiscoveryInboxService:
    """Operator review actions for discovered lead candidates."""

    def __init__(self, repos: DiscoveryInboxRepositories | None = None) -> None:
        self._repos = repos or default_discovery_inbox_repositories()

    def list_candidates(
        self,
        conn: psycopg.Connection,
        *,
        filters: DiscoveryInboxFilters | None = None,
    ) -> list[dict[str, Any]]:
        return self._repos.inbox.list_candidates(conn, filters=filters)

    def list_filter_metadata(self, conn: psycopg.Connection) -> dict[str, Any]:
        return {
            "sources": self._repos.inbox.list_sources(conn),
            "runs": self._repos.inbox.list_runs(conn),
        }

    def get_candidate_detail(
        self,
        conn: psycopg.Connection,
        candidate_id: UUID,
    ) -> dict[str, Any] | None:
        candidate = self._repos.inbox.get_candidate(conn, candidate_id)
        if candidate is None:
            return None
        domain = candidate.get("domain")
        name = str(candidate.get("name") or "")
        company_matches = (
            self._repos.crm.companies.find_by_domain(conn, str(domain))
            if domain
            else []
        )
        name_matches = self._repos.crm.companies.find_by_exact_name(conn, name)
        seen: set[str] = set()
        suggestions: list[dict[str, Any]] = []
        for row in company_matches + name_matches:
            row_id = str(row["id"])
            if row_id in seen:
                continue
            seen.add(row_id)
            suggestions.append(row)
        candidate["match_suggestions"] = suggestions or candidate.get("match_suggestions") or []
        conflicts: list[str] = []
        if suggestions and domain:
            for match in suggestions:
                if match.get("domain") and str(match["domain"]).lower() != str(domain).lower():
                    conflicts.append(
                        f"Domain mismatch with existing company {match.get('name')}"
                    )
        candidate["conflicts"] = conflicts or candidate.get("conflicts") or []
        return candidate

    def accept_candidate(
        self,
        conn: psycopg.Connection,
        *,
        candidate_id: UUID,
        actor_context: ActorContext,
        company_choice: str,
        selected_company_id: UUID | None = None,
    ) -> dict[str, Any]:
        candidate = self._require_pending_candidate(conn, candidate_id)
        with crm_transaction(conn):
            if company_choice == "existing":
                if selected_company_id is None:
                    raise DiscoveryInboxError("selected_company_id is required")
                company = self._repos.crm.companies.get_by_id(conn, selected_company_id)
                if company is None:
                    raise DiscoveryInboxError("Selected company was not found.")
                outcome = "linked"
            else:
                category = crm_category_for_discovery(str(candidate.get("category") or "unclear"))
                company = self._repos.crm.companies.create(
                    conn,
                    name=str(candidate["name"]),
                    website=candidate.get("website"),
                    domain=candidate.get("domain"),
                    category=category,
                )
                self._repos.crm.pipeline.update_pipeline_fields(
                    conn,
                    UUID(str(company["id"])),
                    pipeline_stage="researching",
                )
                outcome = "created"

            source_record = self._repos.crm.source_records.create(
                conn,
                source_type="discovery",
                external_id=str(candidate["external_id"]),
                company_id=UUID(str(company["id"])),
                payload={
                    "candidate_id": str(candidate_id),
                    "source_id": candidate.get("source_id"),
                    "evidence_fingerprint": candidate.get("evidence_fingerprint"),
                    "outcome": outcome,
                },
            )
            self._attach_evidence_records(
                conn,
                candidate=candidate,
                company_id=UUID(str(company["id"])),
                actor_context=actor_context,
            )
            updated = self._repos.inbox.update_candidate_review(
                conn,
                candidate_id,
                review_state="accepted",
                reviewed_by=actor_context.actor,
                linked_company_id=UUID(str(company["id"])),
            )
            audit_service.record_discovery_candidate_accept(
                conn,
                actor_context=actor_context,
                candidate_id=str(candidate_id),
                summary_after={
                    "candidate_id": str(candidate_id),
                    "company_id": str(company["id"]),
                    "source_id": candidate.get("source_id"),
                    "outcome": outcome,
                },
            )
        return {
            "candidate": updated,
            "company": company,
            "source_record": source_record,
        }

    def reject_candidate(
        self,
        conn: psycopg.Connection,
        *,
        candidate_id: UUID,
        actor_context: ActorContext,
        rejection_reason: str,
    ) -> dict[str, Any]:
        candidate = self._require_pending_candidate(conn, candidate_id)
        with crm_transaction(conn):
            updated = self._repos.inbox.update_candidate_review(
                conn,
                candidate_id,
                review_state="rejected",
                reviewed_by=actor_context.actor,
                rejection_reason=rejection_reason.strip(),
            )
            suppression = self._repos.inbox.record_rejection_suppression(
                conn,
                source_id=str(candidate["source_id"]),
                external_id=str(candidate["external_id"]),
                evidence_fingerprint=str(candidate["evidence_fingerprint"]),
                rejection_reason=rejection_reason.strip(),
                rejected_by=actor_context.actor,
                candidate_id=candidate_id,
            )
            audit_service.record_discovery_candidate_reject(
                conn,
                actor_context=actor_context,
                candidate_id=str(candidate_id),
                summary_after={
                    "candidate_id": str(candidate_id),
                    "source_id": candidate.get("source_id"),
                    "external_id": candidate.get("external_id"),
                    "evidence_fingerprint": candidate.get("evidence_fingerprint"),
                    "suppression_id": str(suppression["id"]),
                },
            )
        return {"candidate": updated, "suppression": suppression}

    def defer_candidate(
        self,
        conn: psycopg.Connection,
        *,
        candidate_id: UUID,
        actor_context: ActorContext,
        deferred_until: datetime,
    ) -> dict[str, Any]:
        candidate = self._require_pending_candidate(conn, candidate_id)
        normalized = deferred_until if deferred_until.tzinfo else deferred_until.replace(tzinfo=timezone.utc)
        with crm_transaction(conn):
            updated = self._repos.inbox.update_candidate_review(
                conn,
                candidate_id,
                review_state="deferred",
                reviewed_by=actor_context.actor,
                deferred_until=normalized,
            )
            audit_service.record_discovery_candidate_defer(
                conn,
                actor_context=actor_context,
                candidate_id=str(candidate_id),
                summary_after={
                    "candidate_id": str(candidate_id),
                    "deferred_until": normalized.isoformat(),
                },
            )
        return {"candidate": updated}

    def preview_bulk_action(
        self,
        conn: psycopg.Connection,
        *,
        action: BulkAction,
        candidate_ids: list[UUID],
        rejection_reason: str | None = None,
        deferred_until: datetime | None = None,
    ) -> dict[str, Any]:
        self._validate_bulk_selection(candidate_ids)
        rows = self._repos.inbox.get_candidates_by_ids(conn, candidate_ids)
        found_ids = {UUID(str(row["id"])) for row in rows}
        missing = [str(item) for item in candidate_ids if item not in found_ids]
        if missing:
            raise DiscoveryCandidateNotFoundError(f"Unknown candidates: {', '.join(missing)}")
        invalid = [str(row["id"]) for row in rows if row.get("review_state") != "pending"]
        preview_rows = [
            {
                "id": str(row["id"]),
                "name": row.get("name"),
                "source_id": row.get("source_id"),
                "domain": row.get("domain"),
                "review_state": row.get("review_state"),
            }
            for row in rows
        ]
        token = self._bulk_preview_token(
            action=action,
            candidate_ids=candidate_ids,
            rejection_reason=rejection_reason,
            deferred_until=deferred_until,
        )
        return {
            "action": action,
            "count": len(rows),
            "candidates": preview_rows,
            "invalid_state_ids": invalid,
            "preview_token": token,
            "rejection_reason": rejection_reason,
            "deferred_until": deferred_until.isoformat() if deferred_until else None,
        }

    def commit_bulk_action(
        self,
        conn: psycopg.Connection,
        *,
        actor_context: ActorContext,
        action: BulkAction,
        candidate_ids: list[UUID],
        preview_token: str,
        rejection_reason: str | None = None,
        deferred_until: datetime | None = None,
    ) -> dict[str, Any]:
        self._validate_bulk_selection(candidate_ids)
        expected = self._bulk_preview_token(
            action=action,
            candidate_ids=candidate_ids,
            rejection_reason=rejection_reason,
            deferred_until=deferred_until,
        )
        if preview_token != expected:
            raise DiscoveryInboxError("Bulk preview token does not match the submitted action.")
        results: list[dict[str, Any]] = []
        for candidate_id in candidate_ids:
            if action == "accept":
                results.append(
                    self.accept_candidate(
                        conn,
                        candidate_id=candidate_id,
                        actor_context=actor_context,
                        company_choice="new",
                    )
                )
            elif action == "reject":
                if not rejection_reason or not rejection_reason.strip():
                    raise DiscoveryInboxError("rejection_reason is required for bulk reject")
                results.append(
                    self.reject_candidate(
                        conn,
                        candidate_id=candidate_id,
                        actor_context=actor_context,
                        rejection_reason=rejection_reason.strip(),
                    )
                )
            else:
                if deferred_until is None:
                    raise DiscoveryInboxError("deferred_until is required for bulk defer")
                results.append(
                    self.defer_candidate(
                        conn,
                        candidate_id=candidate_id,
                        actor_context=actor_context,
                        deferred_until=deferred_until,
                    )
                )
        with crm_transaction(conn):
            audit_service.record_discovery_candidate_bulk(
                conn,
                actor_context=actor_context,
                summary_after={
                    "action": action,
                    "count": len(candidate_ids),
                    "candidate_ids": [str(item) for item in candidate_ids],
                },
            )
        return {"action": action, "count": len(results), "results": results}

    def _attach_evidence_records(
        self,
        conn: psycopg.Connection,
        *,
        candidate: dict[str, Any],
        company_id: UUID,
        actor_context: ActorContext,
    ) -> None:
        evidence = candidate.get("evidence")
        if isinstance(evidence, str):
            evidence = json.loads(evidence)
        if not isinstance(evidence, dict):
            return
        observations = evidence.get("observations") or []
        snippet = evidence.get("snippet")
        if snippet:
            record = self._repos.crm.research_records.create(
                conn,
                record_type="public_signal",
                company_id=company_id,
                body=f"Discovery snippet: {snippet}",
                source_name=str(candidate.get("source_id") or "discovery"),
                confidence=candidate.get("confidence"),
            )
            audit_service.record_research_record_create(
                conn,
                actor_context=actor_context,
                research_record_id=str(record["id"]),
                summary_after=audit_service.research_record_audit_summary(
                    research_record_id=str(record["id"]),
                    company_id=str(company_id),
                    contact_id=None,
                    record_type="public_signal",
                    source_name=str(candidate.get("source_id")),
                    confidence=candidate.get("confidence"),
                ),
            )
        for observation in observations:
            if not isinstance(observation, dict):
                continue
            record = self._repos.crm.research_records.create(
                conn,
                record_type="verified_fact",
                company_id=company_id,
                body=str(observation.get("value") or "Discovery observation"),
                source_name=str(observation.get("raw_source_id") or candidate.get("source_id")),
                source_url=observation.get("source_url"),
                confidence=observation.get("confidence"),
                observed_at=observation.get("retrieved_at"),
                review_at=observation.get("review_at"),
                expires_at=observation.get("expires_at"),
            )
            audit_service.record_research_record_create(
                conn,
                actor_context=actor_context,
                research_record_id=str(record["id"]),
                summary_after=audit_service.research_record_audit_summary(
                    research_record_id=str(record["id"]),
                    company_id=str(company_id),
                    contact_id=None,
                    record_type="verified_fact",
                    source_name=str(observation.get("raw_source_id")),
                    source_url=observation.get("source_url"),
                    confidence=observation.get("confidence"),
                    observed_at=observation.get("retrieved_at"),
                    review_at=observation.get("review_at"),
                    expires_at=observation.get("expires_at"),
                ),
            )

    def _require_pending_candidate(
        self,
        conn: psycopg.Connection,
        candidate_id: UUID,
    ) -> dict[str, Any]:
        candidate = self._repos.inbox.get_candidate(conn, candidate_id)
        if candidate is None:
            raise DiscoveryCandidateNotFoundError(str(candidate_id))
        state = str(candidate.get("review_state") or "")
        if state not in {"pending", "deferred"}:
            raise DiscoveryCandidateStateError(
                f"Candidate {candidate_id} is not reviewable (state={state})"
            )
        return candidate

    @staticmethod
    def _validate_bulk_selection(candidate_ids: list[UUID]) -> None:
        if not candidate_ids:
            raise DiscoveryBulkLimitError("Select at least one candidate")
        if len(candidate_ids) > DISCOVERY_BULK_MAX:
            raise DiscoveryBulkLimitError(
                f"Bulk actions are limited to {DISCOVERY_BULK_MAX} candidates"
            )

    @staticmethod
    def _bulk_preview_token(
        *,
        action: BulkAction,
        candidate_ids: list[UUID],
        rejection_reason: str | None,
        deferred_until: datetime | None,
    ) -> str:
        payload = {
            "action": action,
            "candidate_ids": sorted(str(item) for item in candidate_ids),
            "rejection_reason": rejection_reason,
            "deferred_until": deferred_until.isoformat() if deferred_until else None,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
