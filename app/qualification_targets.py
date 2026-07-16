"""Tier A/B/C target lists from deterministic ICP scores.

Qualification tier is distinct from pipeline stage — scored targets are
inspectable lists for prioritization, not automatic outreach promotion.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.companies import COMPANY_CATEGORIES, COMPANY_STAGES, FRESHNESS_FILTERS
from app.icp_scoring import (
    IcpScoreResult,
    IcpScoringRule,
    RULE_STATUS_EXPIRED,
    RULE_STATUS_SCORED,
    calculate_icp_score,
    rule_from_row,
)
from app.pipeline_stages import PIPELINE_STAGES

QUALIFICATION_TIERS: dict[str, tuple[int, int]] = {
    "A": (8, 10),
    "B": (6, 7),
    "C": (4, 5),
}
TIER_ORDER: tuple[str, ...] = ("A", "B", "C")
MIN_ACTIVE_TARGET_SCORE = 4
MAX_WORKING_LIST_ITEMS = 50
WARM_PATH_FILTERS: dict[str, str] = {
    "yes": "Warm path present",
    "no": "No warm path",
}

FreshnessState = Literal["fresh", "stale", "unknown", "mixed"]


class QualificationTargetFilters(BaseModel):
    tier: str | None = None
    category: str | None = None
    stage: str | None = None
    pipeline_stage: str | None = None
    owner: str | None = None
    freshness: str | None = None
    warm_path: str | None = None

    @field_validator("tier")
    @classmethod
    def validate_tier(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        if value not in QUALIFICATION_TIERS:
            raise ValueError(f"unknown tier: {value}")
        return value

    @field_validator("category")
    @classmethod
    def validate_category(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        if value not in COMPANY_CATEGORIES:
            raise ValueError(f"unknown category: {value}")
        return value

    @field_validator("stage")
    @classmethod
    def validate_stage(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        if value not in COMPANY_STAGES:
            raise ValueError(f"unknown stage: {value}")
        return value

    @field_validator("pipeline_stage")
    @classmethod
    def validate_pipeline_stage(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        if value not in PIPELINE_STAGES:
            raise ValueError(f"unknown pipeline_stage: {value}")
        return value

    @field_validator("freshness")
    @classmethod
    def validate_freshness(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        if value not in FRESHNESS_FILTERS:
            raise ValueError(f"unknown freshness: {value}")
        return value

    @field_validator("warm_path")
    @classmethod
    def validate_warm_path(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        if value not in WARM_PATH_FILTERS:
            raise ValueError(f"unknown warm_path filter: {value}")
        return value


@dataclass(frozen=True)
class QualificationTargetRow:
    company_id: str
    name: str
    score: float
    tier: str
    stage: str | None
    vertical: str | None
    strongest_signals: tuple[str, ...]
    warm_path: str | None
    has_warm_path: bool
    next_action: str | None
    evidence_freshness: FreshnessState
    missing_fields: tuple[str, ...]
    pipeline_stage: str | None
    pipeline_owner: str | None
    score_calculated_at: datetime
    tie_breaker_name: str
    stale_evidence: bool

    def sort_key(self) -> tuple[Any, ...]:
        """Deterministic ordering: score desc, tier asc, name asc, id asc."""
        tier_rank = TIER_ORDER.index(self.tier) if self.tier in TIER_ORDER else 99
        return (-self.score, tier_rank, self.tie_breaker_name.lower(), self.company_id)


def tier_for_score(score: float) -> str | None:
    """Map a total score to tier A/B/C, or None when below active threshold."""
    rounded = round(float(score), 2)
    for tier, (low, high) in QUALIFICATION_TIERS.items():
        if low <= rounded <= high:
            return tier
    return None


def is_active_target_score(score: float) -> bool:
    return tier_for_score(score) is not None


def company_freshness_state(
    company: dict[str, Any],
    *,
    reference: date | None = None,
) -> FreshnessState:
    last_verified = company.get("last_verified_at")
    if last_verified is None:
        return "unknown"
    if isinstance(last_verified, datetime):
        verified_date = last_verified.date()
    elif isinstance(last_verified, date):
        verified_date = last_verified
    else:
        return "unknown"
    today = reference or date.today()
    if verified_date >= today - timedelta(days=30):
        return "fresh"
    if verified_date < today - timedelta(days=90):
        return "stale"
    return "mixed"


def _warm_path_summary(score_result: IcpScoreResult) -> tuple[str | None, bool]:
    for item in score_result.breakdown:
        if item.rule_id not in {"warm_path", "decision_maker"}:
            continue
        if item.points_awarded <= 0:
            continue
        for evidence in item.evidence:
            if evidence.get("kind") == "contact":
                name = evidence.get("full_name") or "Contact"
                roles = evidence.get("buying_roles") or []
                role_label = ", ".join(str(role) for role in roles)
                return f"{name} ({role_label})", True
            if evidence.get("record_type") == "relationship_context":
                source = evidence.get("source_name") or "Relationship context"
                return str(source), True
    return None, False


def _strongest_signals(score_result: IcpScoreResult, *, limit: int = 3) -> tuple[str, ...]:
    scored = [
        item
        for item in score_result.breakdown
        if item.points_awarded > 0 and item.status == RULE_STATUS_SCORED
    ]
    scored.sort(key=lambda item: (-item.points_awarded, item.rule_id, item.label))
    return tuple(item.label for item in scored[:limit])


def _has_stale_evidence(score_result: IcpScoreResult) -> bool:
    return any(item.status == RULE_STATUS_EXPIRED for item in score_result.breakdown)


def build_target_row(
    *,
    company: dict[str, Any],
    score_result: IcpScoreResult,
) -> QualificationTargetRow | None:
    tier = tier_for_score(score_result.total_score)
    if tier is None:
        return None
    warm_path, has_warm_path = _warm_path_summary(score_result)
    freshness = company_freshness_state(company)
    if _has_stale_evidence(score_result) and freshness == "fresh":
        freshness = "mixed"
    elif _has_stale_evidence(score_result) and freshness == "unknown":
        freshness = "stale"
    return QualificationTargetRow(
        company_id=str(company["id"]),
        name=str(company.get("name") or ""),
        score=score_result.total_score,
        tier=tier,
        stage=company.get("stage"),
        vertical=company.get("category"),
        strongest_signals=_strongest_signals(score_result),
        warm_path=warm_path,
        has_warm_path=has_warm_path,
        next_action=company.get("next_action"),
        evidence_freshness=freshness,
        missing_fields=tuple(score_result.missing_inputs),
        pipeline_stage=company.get("pipeline_stage"),
        pipeline_owner=company.get("pipeline_owner"),
        score_calculated_at=score_result.calculated_at,
        tie_breaker_name=str(company.get("name") or ""),
        stale_evidence=_has_stale_evidence(score_result),
    )


def sort_target_rows(rows: list[QualificationTargetRow]) -> list[QualificationTargetRow]:
    return sorted(rows, key=lambda row: row.sort_key())


def filter_target_rows(
    rows: list[QualificationTargetRow],
    filters: QualificationTargetFilters,
) -> list[QualificationTargetRow]:
    filtered: list[QualificationTargetRow] = []
    for row in rows:
        if filters.tier and row.tier != filters.tier:
            continue
        if filters.category and row.vertical != filters.category:
            continue
        if filters.stage and row.stage != filters.stage:
            continue
        if filters.pipeline_stage and row.pipeline_stage != filters.pipeline_stage:
            continue
        if filters.owner:
            owner = (row.pipeline_owner or "").strip().lower()
            if owner != filters.owner.strip().lower():
                continue
        if filters.freshness:
            if filters.freshness == "fresh" and row.evidence_freshness != "fresh":
                continue
            if filters.freshness == "stale" and row.evidence_freshness not in {"stale", "mixed"}:
                continue
            if filters.freshness == "unknown" and row.evidence_freshness != "unknown":
                continue
        if filters.warm_path == "yes" and not row.has_warm_path:
            continue
        if filters.warm_path == "no" and row.has_warm_path:
            continue
        filtered.append(row)
    return filtered


def score_company_with_rules(
    *,
    company: dict[str, Any],
    contacts: list[dict[str, Any]],
    research_records: list[dict[str, Any]],
    rules: list[IcpScoringRule],
    version_number: int,
    calculated_at: datetime | None = None,
) -> IcpScoreResult:
    return calculate_icp_score(
        company=company,
        contacts=contacts,
        research_records=research_records,
        rules=rules,
        version_number=version_number,
        calculated_at=calculated_at,
    )


def rules_from_rows(rows: list[dict[str, Any]]) -> list[IcpScoringRule]:
    return [rule_from_row(row) for row in rows]


class WorkingListCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    company_ids: list[str] = Field(default_factory=list)

    @field_validator("company_ids")
    @classmethod
    def bounded_company_ids(cls, value: list[str]) -> list[str]:
        if len(value) > MAX_WORKING_LIST_ITEMS:
            raise ValueError(f"working list cannot exceed {MAX_WORKING_LIST_ITEMS} companies")
        return value


def tier_change_metadata(
    *,
    previous_tier: str | None,
    new_tier: str,
    score: float,
) -> dict[str, Any]:
    return {
        "previous_tier": previous_tier,
        "new_tier": new_tier,
        "score": score,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
