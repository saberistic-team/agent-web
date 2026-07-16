"""Deterministic ICP qualification scoring for acquisition companies.

Transparent ten-point rule set (Saberistic ICP model):

| Rule key           | Dimension         | What it measures                                      |
|--------------------|-------------------|-------------------------------------------------------|
| vertical_fit       | vertical          | Company category matches target verticals             |
| stage_fit          | stage             | Funding stage is in the ICP sweet spot                  |
| funding_recency    | funding_recency   | Recent verified funding signal                        |
| hiring_growth      | hiring_growth     | Headcount band or hiring/growth evidence              |
| technical_trigger  | technical_trigger | Technical infrastructure or platform trigger          |
| warm_path          | warm_path         | Introducer contact or relationship-context evidence   |
| decision_maker     | warm_path         | Qualifying buying-role contact on file                |
| target_disposition | vertical          | Operator marked company as a target                   |
| pipeline_progress  | stage             | Pipeline stage at or beyond qualified                 |
| fresh_evidence     | technical_trigger | Non-stale verified evidence observed recently         |

Each rule contributes up to 1.0 point (editable weight). Total score is capped at 10.
Scores are inspectable: every rule reports evidence used, missing inputs, and status.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.companies import COMPANY_CATEGORIES
from app.contacts import BUYING_ROLES
from app.pipeline_stages import PIPELINE_STAGE_ORDER
from app.research_records import (
    PUBLIC_EVIDENCE_TYPES,
    RESEARCH_RECORD_TYPES,
    is_public_evidence_type,
    is_stale,
    parse_optional_datetime,
)

ICP_DIMENSIONS: frozenset[str] = frozenset(
    {
        "stage",
        "vertical",
        "funding_recency",
        "hiring_growth",
        "technical_trigger",
        "warm_path",
    }
)

RULE_STATUS_SCORED = "scored"
RULE_STATUS_MISSING = "missing_data"
RULE_STATUS_EXPIRED = "expired_only"
RULE_STATUS_HYPOTHESIS = "hypothesis_only"
RULE_STATUS_DISABLED = "disabled"

DECISION_MAKER_ROLES: frozenset[str] = frozenset(
    {"founder", "technical_buyer", "executive_buyer"}
)

TARGET_VERTICALS: tuple[str, ...] = ("fintech", "ai_infrastructure", "digital_assets")
TARGET_STAGES: tuple[str, ...] = ("pre_seed", "seed", "series_a")
PIPELINE_PROGRESS_STAGES: tuple[str, ...] = tuple(
    stage
    for stage in PIPELINE_STAGE_ORDER
    if stage not in {"researching", "lost", "nurture"}
)

FUNDING_KEYWORDS: tuple[str, ...] = (
    "funding",
    "raised",
    "series",
    "seed round",
    "investment",
    "venture",
)
HIRING_KEYWORDS: tuple[str, ...] = (
    "hiring",
    "headcount",
    "open role",
    "job posting",
    "recruiting",
    "growth",
)
TECHNICAL_KEYWORDS: tuple[str, ...] = (
    "api",
    "platform",
    "infrastructure",
    "migration",
    "integration",
    "kubernetes",
    "cloud",
    "data pipeline",
    "mlops",
    "fintech stack",
)


class IcpRuleThreshold(BaseModel):
    """Rule-specific configuration stored as JSONB."""

    categories: list[str] = Field(default_factory=list)
    stages: list[str] = Field(default_factory=list)
    pipeline_stages: list[str] = Field(default_factory=list)
    buying_roles: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    record_types: list[str] = Field(default_factory=list)
    max_days: int | None = None
    min_headcount: int | None = None
    max_headcount: int | None = None
    target_status: str | None = None

    @field_validator("max_days", "min_headcount", "max_headcount")
    @classmethod
    def non_negative_optional(cls, value: int | None) -> int | None:
        if value is not None and value < 0:
            raise ValueError("threshold values must be non-negative")
        return value


class IcpScoringRule(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    dimension: str
    label: str = Field(min_length=1, max_length=200)
    weight: float = Field(ge=0.0, le=10.0)
    threshold: IcpRuleThreshold = Field(default_factory=IcpRuleThreshold)
    enabled: bool = True
    accept_hypothesis: bool = False
    sort_order: int = 0

    @field_validator("dimension")
    @classmethod
    def validate_dimension(cls, value: str) -> str:
        if value not in ICP_DIMENSIONS:
            allowed = ", ".join(sorted(ICP_DIMENSIONS))
            raise ValueError(f"dimension must be one of: {allowed}")
        return value


class RuleContribution(BaseModel):
    rule_id: str
    dimension: str
    label: str
    weight: float
    points_awarded: float
    status: str
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    missing_inputs: list[str] = Field(default_factory=list)


class IcpScoreResult(BaseModel):
    version_number: int
    total_score: float
    computed_score: float
    breakdown: list[RuleContribution]
    missing_inputs: list[str]
    calculated_at: datetime
    is_override: bool = False
    override_reason: str | None = None
    override_by: str | None = None


def default_icp_rules() -> list[IcpScoringRule]:
    """Return the canonical Saberistic ten-point rule set."""
    return [
        IcpScoringRule(
            id="vertical_fit",
            dimension="vertical",
            label="Target vertical",
            weight=1.0,
            threshold=IcpRuleThreshold(categories=list(TARGET_VERTICALS)),
            sort_order=1,
        ),
        IcpScoringRule(
            id="stage_fit",
            dimension="stage",
            label="Funding stage fit",
            weight=1.0,
            threshold=IcpRuleThreshold(stages=list(TARGET_STAGES)),
            sort_order=2,
        ),
        IcpScoringRule(
            id="funding_recency",
            dimension="funding_recency",
            label="Recent funding signal",
            weight=1.0,
            threshold=IcpRuleThreshold(
                keywords=list(FUNDING_KEYWORDS),
                record_types=["verified_fact", "public_signal"],
                max_days=180,
            ),
            sort_order=3,
        ),
        IcpScoringRule(
            id="hiring_growth",
            dimension="hiring_growth",
            label="Hiring / growth signal",
            weight=1.0,
            threshold=IcpRuleThreshold(
                keywords=list(HIRING_KEYWORDS),
                record_types=["verified_fact", "public_signal"],
                min_headcount=10,
                max_headcount=250,
            ),
            sort_order=4,
        ),
        IcpScoringRule(
            id="technical_trigger",
            dimension="technical_trigger",
            label="Technical trigger",
            weight=1.0,
            threshold=IcpRuleThreshold(
                keywords=list(TECHNICAL_KEYWORDS),
                record_types=["verified_fact", "public_signal"],
            ),
            sort_order=5,
        ),
        IcpScoringRule(
            id="warm_path",
            dimension="warm_path",
            label="Warm introduction path",
            weight=1.0,
            threshold=IcpRuleThreshold(
                buying_roles=["introducer"],
                record_types=["relationship_context"],
            ),
            sort_order=6,
        ),
        IcpScoringRule(
            id="decision_maker",
            dimension="warm_path",
            label="Qualifying decision-maker",
            weight=1.0,
            threshold=IcpRuleThreshold(buying_roles=sorted(DECISION_MAKER_ROLES)),
            sort_order=7,
        ),
        IcpScoringRule(
            id="target_disposition",
            dimension="vertical",
            label="Target disposition",
            weight=1.0,
            threshold=IcpRuleThreshold(target_status="target"),
            sort_order=8,
        ),
        IcpScoringRule(
            id="pipeline_progress",
            dimension="stage",
            label="Pipeline engagement",
            weight=1.0,
            threshold=IcpRuleThreshold(pipeline_stages=list(PIPELINE_PROGRESS_STAGES)),
            sort_order=9,
        ),
        IcpScoringRule(
            id="fresh_evidence",
            dimension="technical_trigger",
            label="Fresh verified evidence",
            weight=1.0,
            threshold=IcpRuleThreshold(
                record_types=["verified_fact", "public_signal"],
                max_days=90,
            ),
            sort_order=10,
        ),
    ]


def _coerce_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    if isinstance(value, str):
        return parse_optional_datetime(value)
    return None


def _text_blob(*parts: Any) -> str:
    return " ".join(str(part).lower() for part in parts if part)


def _record_evidence_summary(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(record.get("id", "")),
        "record_type": record.get("record_type"),
        "observed_value": record.get("observed_value"),
        "source_name": record.get("source_name"),
        "observed_at": record.get("observed_at"),
        "expires_at": record.get("expires_at"),
    }


def _classify_record(
    record: dict[str, Any],
    *,
    reference: datetime,
    accept_hypothesis: bool,
) -> Literal["verified", "expired", "hypothesis", "other"]:
    record_type = str(record.get("record_type", ""))
    if record_type == "hypothesis":
        return "verified" if accept_hypothesis else "hypothesis"
    if record_type in PUBLIC_EVIDENCE_TYPES:
        if is_stale(record, now=reference):
            return "expired"
        return "verified"
    if record_type == "relationship_context":
        return "verified"
    return "other"


def _keywords_match(text: str, keywords: list[str]) -> bool:
    lowered = text.lower()
    return any(keyword.lower() in lowered for keyword in keywords)


def _evaluate_rule(
    rule: IcpScoringRule,
    *,
    company: dict[str, Any],
    contacts: list[dict[str, Any]],
    research_records: list[dict[str, Any]],
    reference: datetime,
) -> RuleContribution:
    if not rule.enabled:
        return RuleContribution(
            rule_id=rule.id,
            dimension=rule.dimension,
            label=rule.label,
            weight=rule.weight,
            points_awarded=0.0,
            status=RULE_STATUS_DISABLED,
        )

    threshold = rule.threshold
    missing: list[str] = []
    evidence: list[dict[str, Any]] = []
    saw_expired = False
    saw_hypothesis = False
    matched = False

    if rule.id == "vertical_fit":
        category = company.get("category")
        if not category:
            missing.append("company.category")
        elif category in threshold.categories:
            matched = True
            evidence.append(
                {
                    "kind": "company_field",
                    "field": "category",
                    "value": category,
                    "label": COMPANY_CATEGORIES.get(category, category),
                }
            )

    elif rule.id == "stage_fit":
        stage = company.get("stage")
        if not stage:
            missing.append("company.stage")
        elif stage in threshold.stages:
            matched = True
            evidence.append({"kind": "company_field", "field": "stage", "value": stage})

    elif rule.id == "target_disposition":
        target_status = company.get("target_status")
        if not target_status:
            missing.append("company.target_status")
        elif target_status == threshold.target_status:
            matched = True
            evidence.append(
                {
                    "kind": "company_field",
                    "field": "target_status",
                    "value": target_status,
                }
            )

    elif rule.id == "pipeline_progress":
        pipeline_stage = company.get("pipeline_stage")
        if not pipeline_stage:
            missing.append("company.pipeline_stage")
        elif pipeline_stage in threshold.pipeline_stages:
            matched = True
            evidence.append(
                {
                    "kind": "company_field",
                    "field": "pipeline_stage",
                    "value": pipeline_stage,
                }
            )

    elif rule.id == "decision_maker":
        if not contacts:
            missing.append("contacts")
        for contact in contacts:
            roles = contact.get("buying_roles") or []
            overlap = [role for role in roles if role in threshold.buying_roles]
            if overlap:
                matched = True
                evidence.append(
                    {
                        "kind": "contact",
                        "contact_id": str(contact.get("id", "")),
                        "full_name": contact.get("full_name"),
                        "buying_roles": overlap,
                    }
                )
                break

    elif rule.id == "warm_path":
        if not contacts and not research_records:
            missing.append("contacts_or_research_records")
        for contact in contacts:
            roles = contact.get("buying_roles") or []
            if "introducer" in roles:
                matched = True
                evidence.append(
                    {
                        "kind": "contact",
                        "contact_id": str(contact.get("id", "")),
                        "full_name": contact.get("full_name"),
                        "buying_roles": ["introducer"],
                    }
                )
                break
        for record in research_records:
            classification = _classify_record(
                record, reference=reference, accept_hypothesis=rule.accept_hypothesis
            )
            if classification == "expired":
                saw_expired = True
                continue
            if classification == "hypothesis":
                saw_hypothesis = True
                continue
            if record.get("record_type") == "relationship_context":
                matched = True
                evidence.append(_record_evidence_summary(record))
                break

    elif rule.id == "hiring_growth":
        headcount = company.get("headcount_estimate")
        if headcount is None:
            missing.append("company.headcount_estimate")
        elif (
            threshold.min_headcount is not None
            and threshold.max_headcount is not None
            and threshold.min_headcount <= int(headcount) <= threshold.max_headcount
        ):
            matched = True
            evidence.append(
                {
                    "kind": "company_field",
                    "field": "headcount_estimate",
                    "value": headcount,
                }
            )
        if not matched:
            for record in research_records:
                classification = _classify_record(
                    record, reference=reference, accept_hypothesis=rule.accept_hypothesis
                )
                if classification == "expired":
                    saw_expired = True
                    continue
                if classification == "hypothesis":
                    saw_hypothesis = True
                    continue
                record_type = str(record.get("record_type", ""))
                if (
                    record_type not in threshold.record_types
                    and not (rule.accept_hypothesis and record_type == "hypothesis")
                ):
                    continue
                blob = _text_blob(
                    record.get("observed_value"),
                    record.get("body"),
                    record.get("source_name"),
                )
                if _keywords_match(blob, threshold.keywords):
                    matched = True
                    evidence.append(_record_evidence_summary(record))
                    break

    elif rule.id in {"funding_recency", "technical_trigger", "fresh_evidence"}:
        if not research_records and rule.id != "funding_recency":
            missing.append("research_records")
        if rule.id == "funding_recency" and not research_records and not company.get(
            "funding_summary"
        ):
            missing.append("research_records_or_funding_summary")

        if rule.id == "funding_recency" and company.get("funding_summary"):
            verified_at = _coerce_datetime(company.get("last_verified_at"))
            if verified_at and threshold.max_days is not None:
                if reference - verified_at <= timedelta(days=threshold.max_days):
                    matched = True
                    evidence.append(
                        {
                            "kind": "company_field",
                            "field": "funding_summary",
                            "value": company.get("funding_summary"),
                            "last_verified_at": company.get("last_verified_at"),
                        }
                    )

        for record in research_records:
            classification = _classify_record(
                record, reference=reference, accept_hypothesis=rule.accept_hypothesis
            )
            if classification == "expired":
                saw_expired = True
                continue
            if classification == "hypothesis":
                saw_hypothesis = True
                continue
            if record.get("record_type") not in threshold.record_types:
                continue
            observed_at = _coerce_datetime(record.get("observed_at"))
            if threshold.max_days is not None and observed_at is not None:
                if reference - observed_at > timedelta(days=threshold.max_days):
                    continue
            blob = _text_blob(
                record.get("observed_value"),
                record.get("body"),
                record.get("source_name"),
            )
            if rule.id == "fresh_evidence" or _keywords_match(blob, threshold.keywords):
                matched = True
                evidence.append(_record_evidence_summary(record))
                break

    status = RULE_STATUS_SCORED if matched else RULE_STATUS_MISSING
    if not matched and saw_expired and not saw_hypothesis:
        status = RULE_STATUS_EXPIRED
    elif not matched and saw_hypothesis and not saw_expired:
        status = RULE_STATUS_HYPOTHESIS
    elif not matched and saw_expired and saw_hypothesis:
        status = RULE_STATUS_EXPIRED

    points = rule.weight if matched else 0.0
    return RuleContribution(
        rule_id=rule.id,
        dimension=rule.dimension,
        label=rule.label,
        weight=rule.weight,
        points_awarded=round(points, 2),
        status=status,
        evidence=evidence,
        missing_inputs=missing,
    )


def calculate_icp_score(
    *,
    company: dict[str, Any],
    contacts: list[dict[str, Any]],
    research_records: list[dict[str, Any]],
    rules: list[IcpScoringRule],
    version_number: int,
    calculated_at: datetime | None = None,
    is_override: bool = False,
    override_reason: str | None = None,
    override_by: str | None = None,
    override_score: float | None = None,
) -> IcpScoreResult:
    """Deterministically score a company against an explicit rule set."""
    reference = calculated_at or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)

    ordered_rules = sorted(rules, key=lambda rule: (rule.sort_order, rule.id))
    breakdown = [
        _evaluate_rule(
            rule,
            company=company,
            contacts=contacts,
            research_records=research_records,
            reference=reference,
        )
        for rule in ordered_rules
    ]
    computed_score = round(sum(item.points_awarded for item in breakdown), 2)
    total_score = round(override_score, 2) if is_override and override_score is not None else computed_score
    total_score = min(total_score, 10.0)

    missing_inputs = sorted(
        {
            missing
            for item in breakdown
            for missing in item.missing_inputs
            if item.points_awarded == 0.0
        }
    )

    return IcpScoreResult(
        version_number=version_number,
        total_score=total_score,
        computed_score=computed_score,
        breakdown=breakdown,
        missing_inputs=missing_inputs,
        calculated_at=reference,
        is_override=is_override,
        override_reason=override_reason,
        override_by=override_by,
    )


def rule_from_row(row: dict[str, Any]) -> IcpScoringRule:
    threshold_raw = row.get("threshold") or {}
    if not isinstance(threshold_raw, dict):
        threshold_raw = {}
    return IcpScoringRule(
        id=str(row["id"]),
        dimension=str(row["dimension"]),
        label=str(row["label"]),
        weight=float(row["weight"]),
        threshold=IcpRuleThreshold.model_validate(threshold_raw),
        enabled=bool(row.get("enabled", True)),
        accept_hypothesis=bool(row.get("accept_hypothesis", False)),
        sort_order=int(row.get("sort_order", 0)),
    )


def snapshot_from_result(
    *,
    company_id: str,
    version_id: str,
    result: IcpScoreResult,
) -> dict[str, Any]:
    return {
        "company_id": company_id,
        "version_id": version_id,
        "version_number": result.version_number,
        "total_score": result.total_score,
        "computed_score": result.computed_score,
        "breakdown": [item.model_dump() for item in result.breakdown],
        "missing_inputs": result.missing_inputs,
        "calculated_at": result.calculated_at,
        "is_override": result.is_override,
        "override_reason": result.override_reason,
        "override_by": result.override_by,
    }


def validate_rule_ids(rule_ids: list[str]) -> None:
    expected = {rule.id for rule in default_icp_rules()}
    if set(rule_ids) != expected:
        raise ValueError("rule set must include the canonical ten ICP rules")


def validate_record_types(record_types: list[str]) -> None:
    for record_type in record_types:
        if record_type not in RESEARCH_RECORD_TYPES:
            raise ValueError(f"unknown record_type: {record_type}")
