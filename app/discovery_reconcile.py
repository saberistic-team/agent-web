"""Reconcile discovery candidates with CRM companies while preserving provenance."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID

from app.companies import normalize_company_name, normalize_domain
from app.discovery.category import crm_category_for_discovery, map_suggested_category
from app.discovery.types import DiscoveryCandidate, DiscoveryEvidence, DiscoveryObservation
from app.research_records import parse_optional_datetime

SOURCE_DISCOVERY = "discovery"
SOURCE_MANUAL = "manual"

MatchTier = Literal["source_id", "domain", "domain_alias", "name", "override", "none"]
PreviewOutcome = Literal[
    "matched",
    "create",
    "review",
    "conflict",
    "unchanged",
    "skipped",
]
CommitOutcome = Literal[
    "matched",
    "created",
    "review_queued",
    "conflicted",
    "unchanged",
    "skipped",
]

DISCOVERY_IMPORTABLE_FIELDS = (
    "website",
    "domain",
    "category",
    "funding_summary",
)
USER_PROTECTED_FIELDS = frozenset({"notes", "name"})


@dataclass(frozen=True)
class FieldSource:
    source: str
    run_id: str | None = None
    seen_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "run_id": self.run_id,
            "seen_at": self.seen_at,
        }


@dataclass(frozen=True)
class CompanyMatchCandidate:
    company_id: str
    name: str | None
    domain: str | None
    website: str | None


@dataclass
class MatchResolution:
    tier: MatchTier = "none"
    company: dict[str, Any] | None = None
    requires_review: bool = False
    conflict: bool = False
    reason: str | None = None
    candidates: list[CompanyMatchCandidate] = field(default_factory=list)


@dataclass
class FieldChange:
    field: str
    before: Any
    after: Any


@dataclass
class EvidencePlan:
    append: list[DiscoveryObservation] = field(default_factory=list)
    refresh: list[tuple[str, DiscoveryObservation]] = field(default_factory=list)


@dataclass
class ReconcilePreviewRow:
    row_index: int
    external_id: str
    outcome: PreviewOutcome
    identity: dict[str, Any]
    match_tier: MatchTier
    company_id: str | None = None
    company_label: str | None = None
    field_changes: list[FieldChange] = field(default_factory=list)
    evidence_append_count: int = 0
    evidence_refresh_count: int = 0
    conflict_reason: str | None = None
    conflict_candidates: list[CompanyMatchCandidate] = field(default_factory=list)
    detail: str | None = None


@dataclass
class ReconcilePreview:
    rows: list[ReconcilePreviewRow]
    summary_counts: dict[str, int]
    absent_preserved: int = 0
    review_queue_count: int = 0


def parse_field_sources(raw: Any) -> dict[str, dict[str, Any]]:
    if not raw or not isinstance(raw, dict):
        return {}
    return {str(key): dict(value) for key, value in raw.items() if isinstance(value, dict)}


def is_field_user_owned(field_sources: dict[str, dict[str, Any]], field: str) -> bool:
    entry = field_sources.get(field)
    if entry and entry.get("source") == SOURCE_MANUAL:
        return True
    if field in USER_PROTECTED_FIELDS and entry and entry.get("source") != SOURCE_DISCOVERY:
        return True
    return False


def discovery_field_stamp(*, run_id: str, seen_at: datetime | None = None) -> dict[str, Any]:
    moment = seen_at or datetime.now(timezone.utc)
    return FieldSource(
        source=SOURCE_DISCOVERY,
        run_id=run_id,
        seen_at=moment.isoformat(),
    ).as_dict()


def candidate_identity(candidate: DiscoveryCandidate) -> dict[str, Any]:
    return {
        "external_id": candidate.external_id,
        "name": candidate.name,
        "domain": candidate.domain,
        "website": candidate.website,
        "signals": list(candidate.signals),
    }


def observation_dedup_key(observation: DiscoveryObservation) -> str:
    return f"{observation.source_url}|{observation.raw_source_id}"


def domain_search_keys(domain: str | None) -> tuple[str, ...]:
    if not domain:
        return ()
    keys = [domain]
    parts = domain.split(".")
    if len(parts) > 2:
        keys.append(".".join(parts[1:]))
    return tuple(dict.fromkeys(keys))


def _candidate_from_company(company: dict[str, Any]) -> CompanyMatchCandidate:
    return CompanyMatchCandidate(
        company_id=str(company["id"]),
        name=company.get("name"),
        domain=company.get("domain"),
        website=company.get("website"),
    )


def collect_domain_matches(
    domain: str | None,
    *,
    companies_by_domain: dict[str, list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], MatchTier]:
    if not domain:
        return [], "none"
    matched: dict[str, dict[str, Any]] = {}
    exact = companies_by_domain.get(domain, [])
    for company in exact:
        matched[str(company["id"])] = company
    tier: MatchTier = "domain" if exact else "none"
    if not exact:
        for alias in domain_search_keys(domain)[1:]:
            for company in companies_by_domain.get(alias, []):
                matched[str(company["id"])] = company
        if matched:
            tier = "domain_alias"
    return list(matched.values()), tier


def resolve_company_match(
    candidate: DiscoveryCandidate,
    *,
    source_record: dict[str, Any] | None,
    companies_by_domain: dict[str, list[dict[str, Any]]],
    companies_by_name: dict[str, list[dict[str, Any]]],
    merge_decision: dict[str, Any] | None = None,
    linked_company: dict[str, Any] | None = None,
) -> MatchResolution:
    if merge_decision is not None:
        decision = str(merge_decision.get("decision", ""))
        if decision == "dismiss":
            return MatchResolution(
                tier="none",
                company=None,
                reason="Prior operator dismissal",
            )
        if decision == "link" and merge_decision.get("company_id") and linked_company is not None:
            return MatchResolution(tier="override", company=linked_company)
        if decision == "create":
            return MatchResolution(tier="none", company=None)

    if source_record is not None and source_record.get("company_id") and linked_company is not None:
        return MatchResolution(tier="source_id", company=linked_company)

    domain_matches, domain_tier = collect_domain_matches(
        candidate.domain,
        companies_by_domain=companies_by_domain,
    )
    if len(domain_matches) == 1:
        return MatchResolution(tier=domain_tier, company=domain_matches[0])
    if len(domain_matches) > 1:
        return MatchResolution(
            tier=domain_tier,
            conflict=True,
            reason="Multiple companies share this domain or alias",
            candidates=[_candidate_from_company(item) for item in domain_matches],
        )

    normalized_name = normalize_company_name(candidate.name)
    if normalized_name:
        name_matches = companies_by_name.get(normalized_name.lower(), [])
        if len(name_matches) == 1:
            return MatchResolution(
                tier="name",
                company=name_matches[0],
                requires_review=True,
                reason="Name-only match requires operator review",
            )
        if len(name_matches) > 1:
            return MatchResolution(
                tier="name",
                conflict=True,
                requires_review=True,
                reason="Multiple companies share this name",
                candidates=[_candidate_from_company(item) for item in name_matches],
            )

    return MatchResolution(tier="none", company=None)


def plan_evidence_sync(
    candidate: DiscoveryCandidate,
    *,
    existing_records: list[dict[str, Any]],
) -> EvidencePlan:
    plan = EvidencePlan()
    if candidate.evidence is None:
        return plan
    indexed: dict[str, dict[str, Any]] = {}
    for record in existing_records:
        metadata = record.get("metadata") or {}
        if not isinstance(metadata, dict):
            continue
        key = metadata.get("discovery_observation_key")
        if key:
            indexed[str(key)] = record
    for observation in candidate.evidence.observations:
        key = observation_dedup_key(observation)
        existing = indexed.get(key)
        if existing is None:
            plan.append.append(observation)
        else:
            plan.refresh.append((str(existing["id"]), observation))
    return plan


def compute_discovery_updates(
    company: dict[str, Any] | None,
    candidate: DiscoveryCandidate,
    *,
    run_id: str,
    seen_at: datetime | None = None,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], list[FieldChange]]:
    field_sources = parse_field_sources(company.get("field_sources") if company else None)
    updates: dict[str, Any] = {}
    new_sources = dict(field_sources)
    changes: list[FieldChange] = []
    stamp = discovery_field_stamp(run_id=run_id, seen_at=seen_at)

    suggested_category = crm_category_for_discovery(
        map_suggested_category(tags=list(candidate.signals))
    )
    desired: dict[str, Any] = {
        "website": candidate.website,
        "domain": candidate.domain,
        "category": suggested_category,
        "funding_summary": _funding_summary_from_signals(candidate),
    }

    for field_name, desired_value in desired.items():
        if field_name not in DISCOVERY_IMPORTABLE_FIELDS:
            continue
        if is_field_user_owned(field_sources, field_name):
            continue
        current_value = company.get(field_name) if company else None
        if desired_value in (None, ""):
            continue
        if str(current_value or "") == str(desired_value or ""):
            continue
        if company is None or current_value in (None, "") or field_sources.get(field_name, {}).get("source") == SOURCE_DISCOVERY:
            updates[field_name] = desired_value
            new_sources[field_name] = stamp
            changes.append(
                FieldChange(
                    field=field_name,
                    before=current_value,
                    after=desired_value,
                )
            )

    return updates, new_sources, changes


def _funding_summary_from_signals(candidate: DiscoveryCandidate) -> str | None:
    funding_signals = [signal for signal in candidate.signals if "fund" in signal.lower()]
    if not funding_signals:
        return None
    return "; ".join(funding_signals[:3])


def preview_candidate_row(
    *,
    row_index: int,
    candidate: DiscoveryCandidate,
    match: MatchResolution,
    existing_records: list[dict[str, Any]],
    run_id: str,
    seen_at: datetime | None = None,
) -> ReconcilePreviewRow:
    identity = candidate_identity(candidate)
    if not candidate.name.strip():
        return ReconcilePreviewRow(
            row_index=row_index,
            external_id=candidate.external_id,
            outcome="skipped",
            identity=identity,
            match_tier="none",
            detail="Missing candidate name",
        )

    if match.reason == "Prior operator dismissal":
        return ReconcilePreviewRow(
            row_index=row_index,
            external_id=candidate.external_id,
            outcome="skipped",
            identity=identity,
            match_tier="none",
            detail=match.reason,
        )

    if match.conflict:
        return ReconcilePreviewRow(
            row_index=row_index,
            external_id=candidate.external_id,
            outcome="conflict",
            identity=identity,
            match_tier=match.tier,
            conflict_reason=match.reason,
            conflict_candidates=list(match.candidates),
        )

    if match.requires_review:
        return ReconcilePreviewRow(
            row_index=row_index,
            external_id=candidate.external_id,
            outcome="review",
            identity=identity,
            match_tier=match.tier,
            company_id=str(match.company["id"]) if match.company else None,
            company_label=match.company.get("name") if match.company else None,
            conflict_reason=match.reason,
            conflict_candidates=[_candidate_from_company(match.company)] if match.company else [],
        )

    evidence_plan = plan_evidence_sync(candidate, existing_records=existing_records)
    company = match.company
    updates, _, field_changes = compute_discovery_updates(
        company,
        candidate,
        run_id=run_id,
        seen_at=seen_at,
    )

    if company is None:
        return ReconcilePreviewRow(
            row_index=row_index,
            external_id=candidate.external_id,
            outcome="create",
            identity=identity,
            match_tier=match.tier,
            field_changes=field_changes
            or [FieldChange(field=key, before=None, after=value) for key, value in updates.items()],
            evidence_append_count=len(evidence_plan.append),
            evidence_refresh_count=len(evidence_plan.refresh),
        )

    has_evidence_changes = bool(evidence_plan.append or evidence_plan.refresh)
    if not field_changes and not has_evidence_changes:
        return ReconcilePreviewRow(
            row_index=row_index,
            external_id=candidate.external_id,
            outcome="unchanged",
            identity=identity,
            match_tier=match.tier,
            company_id=str(company["id"]),
            company_label=str(company.get("name") or company["id"]),
        )

    return ReconcilePreviewRow(
        row_index=row_index,
        external_id=candidate.external_id,
        outcome="matched",
        identity=identity,
        match_tier=match.tier,
        company_id=str(company["id"]),
        company_label=str(company.get("name") or company["id"]),
        field_changes=field_changes,
        evidence_append_count=len(evidence_plan.append),
        evidence_refresh_count=len(evidence_plan.refresh),
    )


def build_reconcile_preview(
    candidates: list[DiscoveryCandidate],
    *,
    lookup: Any,
    existing_company_count: int = 0,
    pending_review_count: int = 0,
    run_id: str = "preview",
    seen_at: datetime | None = None,
) -> ReconcilePreview:
    rows: list[ReconcilePreviewRow] = []
    summary = {
        "matched": 0,
        "create": 0,
        "review": 0,
        "conflict": 0,
        "unchanged": 0,
        "skipped": 0,
    }
    for index, candidate in enumerate(candidates):
        match, existing_records = lookup(candidate)
        preview_row = preview_candidate_row(
            row_index=index,
            candidate=candidate,
            match=match,
            existing_records=existing_records,
            run_id=run_id,
            seen_at=seen_at,
        )
        rows.append(preview_row)
        summary[preview_row.outcome] += 1

    touched_ids = {
        row.company_id
        for row in rows
        if row.company_id and row.outcome in {"matched", "unchanged"}
    }
    review_queue_count = pending_review_count + summary["review"] + summary["conflict"]
    return ReconcilePreview(
        rows=rows,
        summary_counts=summary,
        absent_preserved=max(existing_company_count - len(touched_ids), 0),
        review_queue_count=review_queue_count,
    )


def index_companies_by_domain(companies: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    index: dict[str, list[dict[str, Any]]] = {}
    for company in companies:
        domain = company.get("domain")
        if not domain:
            continue
        index.setdefault(str(domain), []).append(company)
    return index


def index_companies_by_name(companies: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    index: dict[str, list[dict[str, Any]]] = {}
    for company in companies:
        name = normalize_company_name(company.get("name"))
        if not name:
            continue
        index.setdefault(name.lower(), []).append(company)
    return index


def observation_to_research_payload(
    observation: DiscoveryObservation,
    *,
    external_id: str,
) -> dict[str, Any]:
    return {
        "record_type": "public_signal",
        "body": observation.value,
        "source_name": "discovery",
        "source_url": observation.source_url,
        "observed_value": observation.value,
        "observed_at": parse_optional_datetime(observation.retrieved_at),
        "confidence": observation.confidence,
        "review_at": parse_optional_datetime(observation.review_at),
        "expires_at": parse_optional_datetime(observation.expires_at),
        "metadata": {
            "discovery_observation_key": observation_dedup_key(observation),
            "discovery_external_id": external_id,
        },
    }


def preview_row_to_dict(row: ReconcilePreviewRow) -> dict[str, Any]:
    return {
        "row_index": row.row_index,
        "external_id": row.external_id,
        "outcome": row.outcome,
        "identity": row.identity,
        "match_tier": row.match_tier,
        "company_id": row.company_id,
        "company_label": row.company_label,
        "field_changes": [
            {"field": change.field, "before": change.before, "after": change.after}
            for change in row.field_changes
        ],
        "evidence_append_count": row.evidence_append_count,
        "evidence_refresh_count": row.evidence_refresh_count,
        "conflict_reason": row.conflict_reason,
        "conflict_candidates": [
            {
                "company_id": candidate.company_id,
                "name": candidate.name,
                "domain": candidate.domain,
                "website": candidate.website,
            }
            for candidate in row.conflict_candidates
        ],
        "detail": row.detail,
    }


def preview_to_dict(preview: ReconcilePreview) -> dict[str, Any]:
    return {
        "rows": [preview_row_to_dict(row) for row in preview.rows],
        "summary_counts": preview.summary_counts,
        "absent_preserved": preview.absent_preserved,
        "review_queue_count": preview.review_queue_count,
    }
