"""CRM orchestration for discovery candidate reconciliation."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import psycopg

from app import audit_service
from app.actor_context import ActorContext
from app.companies import CompanyCreate
from app.crm_lifecycle_audit import record_company_create, record_company_update_if_changed
from app.crm_uow import crm_transaction
from app.discovery.types import DiscoveryCandidate, DiscoveryEvidence, DiscoveryObservation
from app.discovery_reconcile import (
    SOURCE_DISCOVERY,
    build_reconcile_preview,
    candidate_identity,
    compute_discovery_updates,
    index_companies_by_domain,
    index_companies_by_name,
    observation_dedup_key,
    observation_to_research_payload,
    plan_evidence_sync,
    preview_to_dict,
    resolve_company_match,
)
from app.patch import UNSET


def _observation_from_dict(item: dict[str, Any]) -> DiscoveryObservation:
    return DiscoveryObservation(
        source_url=str(item["source_url"]),
        retrieved_at=str(item["retrieved_at"]),
        raw_source_id=str(item["raw_source_id"]),
        value=str(item["value"]),
        confidence=float(item["confidence"]),
        review_at=item.get("review_at"),
        expires_at=item.get("expires_at"),
    )


def candidate_from_payload(payload: dict[str, Any]) -> DiscoveryCandidate:
    evidence_payload = payload.get("evidence")
    evidence = None
    if isinstance(evidence_payload, dict):
        observations = [
            _observation_from_dict(item)
            for item in evidence_payload.get("observations") or []
            if isinstance(item, dict)
        ]
        evidence = DiscoveryEvidence(
            observations=tuple(observations),
            snippet=evidence_payload.get("snippet"),
        )
    return DiscoveryCandidate(
        external_id=str(payload["external_id"]),
        name=str(payload["name"]),
        domain=payload.get("domain"),
        website=payload.get("website"),
        signals=tuple(payload.get("signals") or ()),
        evidence=evidence,
        raw_payload=payload.get("raw_payload"),
    )


def candidates_from_payloads(payloads: list[dict[str, Any]]) -> list[DiscoveryCandidate]:
    return [candidate_from_payload(item) for item in payloads]


def _source_id_for_candidate(candidate: DiscoveryCandidate) -> str:
    if ":" in candidate.external_id:
        return candidate.external_id.split(":", 1)[0]
    return "discovery"


class DiscoveryReconcileOps:
    """Discovery reconciliation persistence helpers used by :class:`CrmService`."""

    def __init__(self, repos: Any) -> None:
        self._repos = repos

    def preview(
        self,
        conn: psycopg.Connection,
        *,
        candidates: list[DiscoveryCandidate],
        run_id: str = "preview",
    ) -> dict[str, Any]:
        companies = self._repos.companies.list_all(conn, limit=5000)
        companies_by_domain = index_companies_by_domain(companies)
        companies_by_name = index_companies_by_name(companies)
        pending_review_count = self._repos.discovery_review.count_pending(conn)

        def lookup(candidate: DiscoveryCandidate) -> tuple[Any, list[dict[str, Any]]]:
            source_record = self._repos.source_records.get_by_source(
                conn,
                source_type=SOURCE_DISCOVERY,
                external_id=candidate.external_id,
            )
            merge_decision = self._repos.discovery_merge_decisions.get_latest(
                conn,
                external_id=candidate.external_id,
            )
            linked_company = None
            if source_record and source_record.get("company_id"):
                linked_company = self._repos.companies.get_by_id(
                    conn, UUID(str(source_record["company_id"]))
                )
            elif merge_decision and merge_decision.get("company_id"):
                linked_company = self._repos.companies.get_by_id(
                    conn, UUID(str(merge_decision["company_id"]))
                )
            match = resolve_company_match(
                candidate,
                source_record=source_record,
                companies_by_domain=companies_by_domain,
                companies_by_name=companies_by_name,
                merge_decision=merge_decision,
                linked_company=linked_company,
            )
            existing_records: list[dict[str, Any]] = []
            if match.company is not None:
                existing_records = self._repos.research_records.list_for_company(
                    conn,
                    UUID(str(match.company["id"])),
                    limit=500,
                )
            return match, existing_records

        preview = build_reconcile_preview(
            candidates,
            lookup=lookup,
            existing_company_count=len(companies),
            pending_review_count=pending_review_count,
            run_id=run_id,
        )
        return preview_to_dict(preview)

    def commit(
        self,
        conn: psycopg.Connection,
        *,
        actor_context: ActorContext,
        candidates: list[DiscoveryCandidate],
        run_id: str,
        merge_decisions: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        summary = {
            "matched": 0,
            "created": 0,
            "review_queued": 0,
            "conflicted": 0,
            "unchanged": 0,
            "skipped": 0,
        }
        rows: list[dict[str, Any]] = []
        seen_at = datetime.now(timezone.utc)
        decision_index = {
            str(item["external_id"]): item
            for item in (merge_decisions or [])
            if item.get("external_id")
        }

        with crm_transaction(conn):
            for candidate in candidates:
                override = decision_index.get(candidate.external_id)
                if override is not None:
                    self.record_merge_decision(
                        conn,
                        actor_context=actor_context,
                        external_id=candidate.external_id,
                        source_id=_source_id_for_candidate(candidate),
                        decision=str(override["decision"]),
                        company_id=override.get("company_id"),
                        candidate=candidate,
                        match_tier=str(override.get("match_tier") or "none"),
                        notes=override.get("notes"),
                    )

            preview = self.preview(conn, candidates=candidates, run_id=run_id)
            for preview_row in preview["rows"]:
                row_index = int(preview_row["row_index"])
                candidate = candidates[row_index]
                outcome = str(preview_row["outcome"])
                if outcome == "skipped":
                    summary["skipped"] += 1
                    rows.append({"row_index": row_index, "outcome": "skipped"})
                    continue
                if outcome in {"review", "conflict"}:
                    self._enqueue_review(conn, candidate=candidate, preview_row=preview_row)
                    key = "review_queued" if outcome == "review" else "conflicted"
                    summary[key] += 1
                    rows.append(
                        {
                            "row_index": row_index,
                            "outcome": outcome,
                            "external_id": preview_row["external_id"],
                        }
                    )
                    continue
                if outcome == "create":
                    created = self._create_company_from_candidate(
                        conn,
                        actor_context=actor_context,
                        candidate=candidate,
                        run_id=run_id,
                        seen_at=seen_at,
                    )
                    summary["created"] += 1
                    rows.append(
                        {
                            "row_index": row_index,
                            "outcome": "created",
                            "company_id": str(created["id"]),
                        }
                    )
                    continue

                company_id = preview_row.get("company_id")
                if not company_id:
                    summary["skipped"] += 1
                    rows.append({"row_index": row_index, "outcome": "skipped"})
                    continue

                company = self._repos.companies.get_by_id(conn, UUID(str(company_id)))
                if company is None:
                    summary["skipped"] += 1
                    rows.append({"row_index": row_index, "outcome": "skipped"})
                    continue

                self._ensure_source_record(
                    conn,
                    candidate=candidate,
                    company_id=UUID(str(company_id)),
                )
                evidence_counts = self._sync_evidence(
                    conn,
                    actor_context=actor_context,
                    candidate=candidate,
                    company_id=UUID(str(company_id)),
                )
                updates, field_sources, _ = compute_discovery_updates(
                    company,
                    candidate,
                    run_id=run_id,
                    seen_at=seen_at,
                )
                if updates or field_sources != (company.get("field_sources") or {}):
                    updated = self._repos.companies.update(
                        conn,
                        UUID(str(company_id)),
                        **updates,
                        field_sources=field_sources if field_sources else UNSET,
                    )
                    if updated is not None:
                        record_company_update_if_changed(
                            conn,
                            actor_context=actor_context,
                            entity_id=str(company_id),
                            before_row=company,
                            after_row=updated,
                        )

                if outcome == "unchanged" and not evidence_counts["append"] and not evidence_counts["refresh"]:
                    summary["unchanged"] += 1
                else:
                    summary["matched"] += 1
                rows.append(
                    {
                        "row_index": row_index,
                        "outcome": outcome,
                        "company_id": str(company_id),
                        "evidence_append_count": evidence_counts["append"],
                        "evidence_refresh_count": evidence_counts["refresh"],
                    }
                )

        return {
            "rows": rows,
            "summary_counts": summary,
            "absent_preserved": preview["absent_preserved"],
            "review_queue_count": preview["review_queue_count"],
        }

    def record_merge_decision(
        self,
        conn: psycopg.Connection,
        *,
        actor_context: ActorContext,
        external_id: str,
        source_id: str,
        decision: str,
        company_id: str | UUID | None,
        candidate: DiscoveryCandidate,
        match_tier: str,
        notes: str | None = None,
    ) -> dict[str, Any]:
        company_uuid = UUID(str(company_id)) if company_id else None
        recorded = self._repos.discovery_merge_decisions.create(
            conn,
            external_id=external_id,
            source_id=source_id,
            decision=decision,
            company_id=company_uuid,
            candidate_domain=candidate.domain,
            candidate_name=candidate.name,
            match_tier=match_tier,
            actor=actor_context.actor,
            correlation_id=actor_context.correlation_id,
            notes=notes,
        )
        audit_service.record_discovery_merge_decision(
            conn,
            actor_context=actor_context,
            external_id=external_id,
            decision=decision,
            company_id=str(company_uuid) if company_uuid else None,
            match_tier=match_tier,
        )
        if decision in {"link", "dismiss", "create"}:
            self._repos.discovery_review.resolve(
                conn,
                external_id=external_id,
                company_id=company_uuid,
                resolved_by=actor_context.actor,
            )
        return recorded

    def list_review_queue(
        self,
        conn: psycopg.Connection,
        *,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return self._repos.discovery_review.list_pending(conn, limit=limit)

    def _enqueue_review(
        self,
        conn: psycopg.Connection,
        *,
        candidate: DiscoveryCandidate,
        preview_row: dict[str, Any],
    ) -> None:
        self._repos.discovery_review.upsert_pending(
            conn,
            external_id=candidate.external_id,
            source_id=_source_id_for_candidate(candidate),
            candidate_name=candidate.name,
            candidate_domain=candidate.domain,
            candidate_payload=candidate_identity(candidate),
            reason=str(preview_row.get("conflict_reason") or preview_row.get("outcome")),
            match_tier=str(preview_row.get("match_tier") or "none"),
            candidate_company_ids=[
                item["company_id"] for item in preview_row.get("conflict_candidates") or []
            ]
            or ([preview_row["company_id"]] if preview_row.get("company_id") else []),
        )

    def _create_company_from_candidate(
        self,
        conn: psycopg.Connection,
        *,
        actor_context: ActorContext,
        candidate: DiscoveryCandidate,
        run_id: str,
        seen_at: datetime,
    ) -> dict[str, Any]:
        updates, field_sources, _ = compute_discovery_updates(
            None,
            candidate,
            run_id=run_id,
            seen_at=seen_at,
        )
        company = CompanyCreate(
            name=candidate.name,
            website=updates.get("website") or candidate.website,
            domain=updates.get("domain") or candidate.domain,
            category=updates.get("category"),
            funding_summary=updates.get("funding_summary"),
        )
        created = self._repos.companies.create(
            conn,
            **company.model_dump(),
            field_sources=field_sources,
        )
        record_company_create(conn, actor_context=actor_context, company=created)
        company_id = UUID(str(created["id"]))
        self._ensure_source_record(conn, candidate=candidate, company_id=company_id)
        self._sync_evidence(
            conn,
            actor_context=actor_context,
            candidate=candidate,
            company_id=company_id,
        )
        return created

    def _ensure_source_record(
        self,
        conn: psycopg.Connection,
        *,
        candidate: DiscoveryCandidate,
        company_id: UUID,
    ) -> dict[str, Any]:
        existing = self._repos.source_records.get_by_source(
            conn,
            source_type=SOURCE_DISCOVERY,
            external_id=candidate.external_id,
        )
        payload = {
            "identity": candidate_identity(candidate),
            "raw_payload": candidate.raw_payload,
        }
        if existing is None:
            return self._repos.source_records.create(
                conn,
                source_type=SOURCE_DISCOVERY,
                external_id=candidate.external_id,
                company_id=company_id,
                payload=payload,
            )
        if str(existing.get("company_id")) != str(company_id):
            return existing
        return self._repos.source_records.update_payload(
            conn,
            record_id=UUID(str(existing["id"])),
            payload=payload,
        )

    def _sync_evidence(
        self,
        conn: psycopg.Connection,
        *,
        actor_context: ActorContext,
        candidate: DiscoveryCandidate,
        company_id: UUID,
    ) -> dict[str, int]:
        existing_records = self._repos.research_records.list_for_company(conn, company_id, limit=500)
        plan = plan_evidence_sync(candidate, existing_records=existing_records)
        append_count = 0
        refresh_count = 0
        for observation in plan.append:
            payload = observation_to_research_payload(observation, external_id=candidate.external_id)
            record = self._repos.research_records.create(conn, company_id=company_id, **payload)
            append_count += 1
            audit_service.record_research_record_create(
                conn,
                actor_context=actor_context,
                research_record_id=str(record["id"]),
                summary_after={
                    "company_id": str(company_id),
                    "discovery_external_id": candidate.external_id,
                    "discovery_observation_key": payload["metadata"]["discovery_observation_key"],
                },
            )
        for record_id, observation in plan.refresh:
            payload = observation_to_research_payload(observation, external_id=candidate.external_id)
            self._repos.research_records.update_freshness(
                conn,
                record_id=UUID(str(record_id)),
                observed_at=payload["observed_at"],
                confidence=observation.confidence,
                review_at=payload["review_at"],
                expires_at=payload["expires_at"],
                metadata={
                    "discovery_observation_key": observation_dedup_key(observation),
                    "discovery_external_id": candidate.external_id,
                },
            )
            refresh_count += 1
        return {"append": append_count, "refresh": refresh_count}
