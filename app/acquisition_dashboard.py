"""Acquisition dashboard metrics — explicit definitions and load helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import psycopg

from app.companies import COMPANY_CATEGORIES, COMPANY_STAGES
from app.repositories.protocols import AcquisitionDashboardRepository

# Bounded list sizes for dashboard sections (indexed queries + LIMIT).
DEFAULT_LIST_LIMIT = 20
UPCOMING_ACTION_WINDOW_DAYS = 14
DASHBOARD_REFERENCE_TIMEZONE = "UTC"

# Non-archived companies included in rollups unless noted otherwise.
METRIC_COMPANY_COUNT_BY_STAGE = (
    "Count of non-archived companies grouped by funding/lifecycle stage "
    "(companies.stage — not pipeline_stage); NULL stage is reported as unspecified."
)
METRIC_COMPANY_COUNT_BY_CATEGORY = (
    "Count of non-archived companies grouped by companies.category; "
    "NULL category is reported as unspecified."
)
METRIC_CONTACT_COUNT_BY_STAGE = (
    "Count of active (non-archived) contacts whose linked company is non-archived, "
    "grouped by that company's funding/lifecycle stage (companies.stage); "
    "NULL stage is unspecified."
)
METRIC_CONTACT_COUNT_BY_CATEGORY = (
    "Count of active (non-archived) contacts whose linked company is non-archived, "
    "grouped by that company's category; NULL category is unspecified."
)
METRIC_OVERDUE_NEXT_ACTION = (
    "Non-archived pipeline companies (companies.pipeline_stage IS NOT NULL) with "
    "non-empty companies.next_action and companies.next_action_due_at strictly "
    f"before the dashboard reference time ({DASHBOARD_REFERENCE_TIMEZONE}). "
    "Uses idx_companies_next_action_due_at; research follow_up_note rows are "
    "historical evidence only."
)
METRIC_UPCOMING_NEXT_ACTION = (
    "Same pipeline inclusion as overdue: non-archived companies with "
    "pipeline_stage, non-empty next_action, and next_action_due_at on or after "
    f"the reference time ({DASHBOARD_REFERENCE_TIMEZONE}) and within "
    f"{UPCOMING_ACTION_WINDOW_DAYS} calendar days (inclusive window end). "
    "Uses idx_companies_next_action_due_at."
)
METRIC_RECENT_EVIDENCE = (
    "Public evidence (verified_fact or public_signal) ordered by created_at "
    "descending — most recently added first."
)
METRIC_STALE_EVIDENCE = (
    "Public evidence with expires_at on or before the reference time — "
    "requires operator re-verification."
)
METRIC_WITHOUT_DECISION_MAKER = (
    "Non-archived companies with target_status target or watching that lack "
    "an active contact in a qualifying decision-maker buying role "
    "(founder, technical buyer, or executive buyer)."
)
METRIC_WITHOUT_NEXT_ACTION = (
    "Non-archived pipeline companies (companies.pipeline_stage IS NOT NULL) "
    "missing a canonical next action: companies.next_action is null/blank or "
    "companies.next_action_due_at is null. Research follow_up_note rows do not "
    "satisfy this metric."
)

CompanyDimension = Literal["stage", "category"]
ContactDimension = Literal["stage", "category"]

_DIMENSION_LABELS: dict[str, dict[str, str]] = {
    "stage": COMPANY_STAGES,
    "category": COMPANY_CATEGORIES,
}


@dataclass(frozen=True)
class CountBucket:
    key: str
    label: str
    count: int


@dataclass(frozen=True)
class NextActionRow:
    company_id: str
    company_name: str
    pipeline_stage: str | None
    pipeline_owner: str | None
    next_action: str
    next_action_due_at: datetime


@dataclass(frozen=True)
class EvidenceRow:
    record_id: str
    company_id: str
    company_name: str
    record_type: str
    body: str
    created_at: datetime
    expires_at: datetime | None


@dataclass(frozen=True)
class CompanyAttentionRow:
    company_id: str
    company_name: str
    target_status: str | None
    category: str | None
    stage: str | None
    pipeline_stage: str | None = None


@dataclass(frozen=True)
class AcquisitionDashboardData:
    company_counts_by_stage: tuple[CountBucket, ...]
    company_counts_by_category: tuple[CountBucket, ...]
    contact_counts_by_stage: tuple[CountBucket, ...]
    contact_counts_by_category: tuple[CountBucket, ...]
    overdue_actions: tuple[NextActionRow, ...]
    upcoming_actions: tuple[NextActionRow, ...]
    recent_evidence: tuple[EvidenceRow, ...]
    stale_evidence: tuple[EvidenceRow, ...]
    without_decision_maker: tuple[CompanyAttentionRow, ...]
    without_next_action: tuple[CompanyAttentionRow, ...]
    generated_at: datetime
    metric_definitions: dict[str, str] = field(default_factory=lambda: {
        "company_count_by_stage": METRIC_COMPANY_COUNT_BY_STAGE,
        "company_count_by_category": METRIC_COMPANY_COUNT_BY_CATEGORY,
        "contact_count_by_stage": METRIC_CONTACT_COUNT_BY_STAGE,
        "contact_count_by_category": METRIC_CONTACT_COUNT_BY_CATEGORY,
        "overdue_next_action": METRIC_OVERDUE_NEXT_ACTION,
        "upcoming_next_action": METRIC_UPCOMING_NEXT_ACTION,
        "recent_evidence": METRIC_RECENT_EVIDENCE,
        "stale_evidence": METRIC_STALE_EVIDENCE,
        "without_decision_maker": METRIC_WITHOUT_DECISION_MAKER,
        "without_next_action": METRIC_WITHOUT_NEXT_ACTION,
    })


def _bucket_label(dimension: CompanyDimension | ContactDimension, key: str) -> str:
    if key == "unspecified":
        return "Unspecified"
    registry = _DIMENSION_LABELS.get(dimension, {})
    return registry.get(key, key.replace("_", " ").title())


def _to_buckets(
    rows: list[tuple[str, int]],
    dimension: CompanyDimension | ContactDimension,
) -> tuple[CountBucket, ...]:
    return tuple(
        CountBucket(key=key, label=_bucket_label(dimension, key), count=count)
        for key, count in rows
    )


def _parse_next_action(row: dict[str, Any]) -> NextActionRow:
    due_at = row["next_action_due_at"]
    if isinstance(due_at, str):
        due_at = datetime.fromisoformat(due_at.replace("Z", "+00:00"))
    if due_at.tzinfo is None:
        due_at = due_at.replace(tzinfo=timezone.utc)
    return NextActionRow(
        company_id=str(row["id"]),
        company_name=str(row.get("name") or row.get("company_name") or ""),
        pipeline_stage=row.get("pipeline_stage"),
        pipeline_owner=row.get("pipeline_owner"),
        next_action=str(row.get("next_action") or ""),
        next_action_due_at=due_at,
    )


def _parse_evidence(row: dict[str, Any]) -> EvidenceRow:
    created_at = row["created_at"]
    if isinstance(created_at, str):
        created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    expires_at = row.get("expires_at")
    if isinstance(expires_at, str):
        expires_at = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return EvidenceRow(
        record_id=str(row["id"]),
        company_id=str(row["company_id"]),
        company_name=str(row.get("company_name") or ""),
        record_type=str(row.get("record_type") or ""),
        body=str(row.get("body") or ""),
        created_at=created_at,
        expires_at=expires_at,
    )


def _parse_attention(row: dict[str, Any]) -> CompanyAttentionRow:
    return CompanyAttentionRow(
        company_id=str(row["id"]),
        company_name=str(row.get("name") or ""),
        target_status=row.get("target_status"),
        category=row.get("category"),
        stage=row.get("stage"),
        pipeline_stage=row.get("pipeline_stage"),
    )


def load_acquisition_dashboard(
    conn: psycopg.Connection,
    repo: AcquisitionDashboardRepository,
    *,
    now: datetime | None = None,
    list_limit: int = DEFAULT_LIST_LIMIT,
) -> AcquisitionDashboardData:
    """Load all acquisition dashboard sections from the repository."""
    reference = now or datetime.now(timezone.utc)
    upcoming_end = reference + timedelta(days=UPCOMING_ACTION_WINDOW_DAYS)

    return AcquisitionDashboardData(
        company_counts_by_stage=_to_buckets(
            repo.count_companies_by_dimension(conn, "stage"), "stage"
        ),
        company_counts_by_category=_to_buckets(
            repo.count_companies_by_dimension(conn, "category"), "category"
        ),
        contact_counts_by_stage=_to_buckets(
            repo.count_contacts_by_company_dimension(conn, "stage"), "stage"
        ),
        contact_counts_by_category=_to_buckets(
            repo.count_contacts_by_company_dimension(conn, "category"), "category"
        ),
        overdue_actions=tuple(
            _parse_next_action(row)
            for row in repo.list_overdue_next_actions(conn, reference=reference, limit=list_limit)
        ),
        upcoming_actions=tuple(
            _parse_next_action(row)
            for row in repo.list_upcoming_next_actions(
                conn,
                reference=reference,
                window_end=upcoming_end,
                limit=list_limit,
            )
        ),
        recent_evidence=tuple(
            _parse_evidence(row)
            for row in repo.list_recent_evidence(conn, limit=list_limit)
        ),
        stale_evidence=tuple(
            _parse_evidence(row)
            for row in repo.list_stale_evidence(conn, reference=reference, limit=list_limit)
        ),
        without_decision_maker=tuple(
            _parse_attention(row)
            for row in repo.list_companies_without_decision_maker(conn, limit=list_limit)
        ),
        without_next_action=tuple(
            _parse_attention(row)
            for row in repo.list_companies_without_next_action(conn, limit=list_limit)
        ),
        generated_at=reference,
    )


def dashboard_is_empty(data: AcquisitionDashboardData) -> bool:
    """True when every rollup and attention list is empty."""
    rollups = (
        data.company_counts_by_stage,
        data.company_counts_by_category,
        data.contact_counts_by_stage,
        data.contact_counts_by_category,
    )
    lists = (
        data.overdue_actions,
        data.upcoming_actions,
        data.recent_evidence,
        data.stale_evidence,
        data.without_decision_maker,
        data.without_next_action,
    )
    return all(not buckets for buckets in rollups) and all(not items for items in lists)
