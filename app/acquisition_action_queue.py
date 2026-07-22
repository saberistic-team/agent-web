"""Daily acquisition action queue — prioritization rules and load helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import psycopg

from app.repositories.protocols import ActionQueueRepository

QUEUE_REFERENCE_TIMEZONE = "UTC"
DEFAULT_QUEUE_LIMIT = 50

# Interim Tier A until scoring (#125) lands: target accounts recently qualified.
TIER_A_RECENT_QUALIFIED_DAYS = 14
TIER_A_PIPELINE_STAGES = frozenset({"qualified", "ready_for_outreach"})
HIGH_VALUE_MIN_CENTS = 50_000

QUEUE_CATEGORY_OVERDUE = "overdue_action"
QUEUE_CATEGORY_DUE_TODAY = "due_today_action"
QUEUE_CATEGORY_TIER_A = "tier_a_qualified"
QUEUE_CATEGORY_WARM_INTRO = "warm_introduction"
QUEUE_CATEGORY_STALE_EVIDENCE = "stale_high_value_evidence"

QueueCategory = Literal[
    "overdue_action",
    "due_today_action",
    "tier_a_qualified",
    "warm_introduction",
    "stale_high_value_evidence",
]

PRIORITY_RANK: dict[str, int] = {
    QUEUE_CATEGORY_OVERDUE: 1,
    QUEUE_CATEGORY_DUE_TODAY: 2,
    QUEUE_CATEGORY_TIER_A: 3,
    QUEUE_CATEGORY_WARM_INTRO: 4,
    QUEUE_CATEGORY_STALE_EVIDENCE: 5,
}

RULE_OVERDUE_ACTION = (
    "Non-archived pipeline companies with non-empty next_action and "
    f"next_action_due_at strictly before the queue reference time ({QUEUE_REFERENCE_TIMEZONE})."
)
RULE_DUE_TODAY_ACTION = (
    "Same pipeline inclusion as overdue, but next_action_due_at falls on the reference "
    f"calendar day in {QUEUE_REFERENCE_TIMEZONE} (inclusive start, exclusive next day)."
)
RULE_TIER_A_QUALIFIED = (
    "Interim Tier A (until scoring #125): target_status = target, pipeline_stage in "
    f"{sorted(TIER_A_PIPELINE_STAGES)}, and a pipeline_stage_history transition to "
    f"'qualified' within the last {TIER_A_RECENT_QUALIFIED_DAYS} calendar days."
)
RULE_WARM_INTRODUCTION = (
    "Active contact on a non-archived company with introducer buying role or warm/strong/"
    "champion relationship strength; company is in pipeline or target/watching."
)
RULE_STALE_HIGH_VALUE_EVIDENCE = (
    "Public evidence (verified_fact or public_signal) with expires_at on or before the "
    f"reference time, for companies with expected_value_cents >= {HIGH_VALUE_MIN_CENTS} "
    "or target_status = target."
)

QUEUE_PRIORITIZATION_RULES: dict[str, str] = {
    QUEUE_CATEGORY_OVERDUE: RULE_OVERDUE_ACTION,
    QUEUE_CATEGORY_DUE_TODAY: RULE_DUE_TODAY_ACTION,
    QUEUE_CATEGORY_TIER_A: RULE_TIER_A_QUALIFIED,
    QUEUE_CATEGORY_WARM_INTRO: RULE_WARM_INTRODUCTION,
    QUEUE_CATEGORY_STALE_EVIDENCE: RULE_STALE_HIGH_VALUE_EVIDENCE,
}


@dataclass(frozen=True)
class ActionQueueItem:
    item_key: str
    priority_rank: int
    category: QueueCategory
    reason: str
    company_id: str
    company_name: str
    contact_id: str | None = None
    contact_name: str | None = None
    next_action: str | None = None
    next_action_due_at: datetime | None = None
    pipeline_stage: str | None = None
    pipeline_owner: str | None = None
    expected_value_cents: int | None = None
    evidence_record_id: str | None = None
    evidence_confidence: float | None = None
    evidence_source_url: str | None = None
    qualified_at: datetime | None = None


@dataclass(frozen=True)
class ActionQueueData:
    items: tuple[ActionQueueItem, ...]
    generated_at: datetime
    rules: dict[str, str] = field(default_factory=lambda: dict(QUEUE_PRIORITIZATION_RULES))


def _utc_day_bounds(reference: datetime) -> tuple[datetime, datetime]:
    """Return [start, end) for the reference calendar day in UTC."""
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    start = reference.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return start, end


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def _item_key(category: str, company_id: str, *, contact_id: str | None = None, record_id: str | None = None) -> str:
    parts = [category, company_id]
    if contact_id:
        parts.append(contact_id)
    if record_id:
        parts.append(record_id)
    return ":".join(parts)


def _reason_overdue(company_name: str, due_at: datetime) -> str:
    return f"Overdue next action for {company_name} — due {due_at.strftime('%Y-%m-%d %H:%M UTC')}."


def _reason_due_today(company_name: str) -> str:
    return f"Next action due today for {company_name}."


def _reason_tier_a(company_name: str, qualified_at: datetime | None) -> str:
    when = qualified_at.strftime("%Y-%m-%d") if qualified_at else "recently"
    return f"Newly qualified Tier A target {company_name} (qualified {when})."


def _reason_warm_intro(contact_name: str, company_name: str, strength: str | None) -> str:
    label = strength.replace("_", " ") if strength else "warm"
    return f"Warm introduction path via {contact_name} at {company_name} ({label} relationship)."


def _reason_stale_evidence(company_name: str, confidence: float | None) -> str:
    conf = f"{confidence:.0%}" if confidence is not None else "unknown"
    return f"Stale high-value evidence for {company_name} — confidence {conf}, needs re-verification."


def _parse_overdue_row(row: dict[str, Any]) -> ActionQueueItem:
    due_at = _parse_dt(row["next_action_due_at"])
    assert due_at is not None
    company_id = str(row["id"])
    company_name = str(row.get("name") or "")
    return ActionQueueItem(
        item_key=_item_key(QUEUE_CATEGORY_OVERDUE, company_id),
        priority_rank=PRIORITY_RANK[QUEUE_CATEGORY_OVERDUE],
        category=QUEUE_CATEGORY_OVERDUE,
        reason=_reason_overdue(company_name, due_at),
        company_id=company_id,
        company_name=company_name,
        next_action=str(row.get("next_action") or ""),
        next_action_due_at=due_at,
        pipeline_stage=row.get("pipeline_stage"),
        pipeline_owner=row.get("pipeline_owner"),
        expected_value_cents=row.get("expected_value_cents"),
    )


def _parse_due_today_row(row: dict[str, Any]) -> ActionQueueItem:
    due_at = _parse_dt(row["next_action_due_at"])
    company_id = str(row["id"])
    company_name = str(row.get("name") or "")
    return ActionQueueItem(
        item_key=_item_key(QUEUE_CATEGORY_DUE_TODAY, company_id),
        priority_rank=PRIORITY_RANK[QUEUE_CATEGORY_DUE_TODAY],
        category=QUEUE_CATEGORY_DUE_TODAY,
        reason=_reason_due_today(company_name),
        company_id=company_id,
        company_name=company_name,
        next_action=str(row.get("next_action") or ""),
        next_action_due_at=due_at,
        pipeline_stage=row.get("pipeline_stage"),
        pipeline_owner=row.get("pipeline_owner"),
        expected_value_cents=row.get("expected_value_cents"),
    )


def _parse_tier_a_row(row: dict[str, Any]) -> ActionQueueItem:
    company_id = str(row["id"])
    company_name = str(row.get("name") or "")
    qualified_at = _parse_dt(row.get("qualified_at"))
    return ActionQueueItem(
        item_key=_item_key(QUEUE_CATEGORY_TIER_A, company_id),
        priority_rank=PRIORITY_RANK[QUEUE_CATEGORY_TIER_A],
        category=QUEUE_CATEGORY_TIER_A,
        reason=_reason_tier_a(company_name, qualified_at),
        company_id=company_id,
        company_name=company_name,
        pipeline_stage=row.get("pipeline_stage"),
        pipeline_owner=row.get("pipeline_owner"),
        expected_value_cents=row.get("expected_value_cents"),
        qualified_at=qualified_at,
    )


def _parse_warm_intro_row(row: dict[str, Any]) -> ActionQueueItem:
    company_id = str(row["company_id"])
    contact_id = str(row["contact_id"])
    contact_name = str(row.get("contact_name") or "")
    company_name = str(row.get("company_name") or "")
    return ActionQueueItem(
        item_key=_item_key(QUEUE_CATEGORY_WARM_INTRO, company_id, contact_id=contact_id),
        priority_rank=PRIORITY_RANK[QUEUE_CATEGORY_WARM_INTRO],
        category=QUEUE_CATEGORY_WARM_INTRO,
        reason=_reason_warm_intro(contact_name, company_name, row.get("relationship_strength")),
        company_id=company_id,
        company_name=company_name,
        contact_id=contact_id,
        contact_name=contact_name,
        pipeline_stage=row.get("pipeline_stage"),
        expected_value_cents=row.get("expected_value_cents"),
    )


def _parse_stale_evidence_row(row: dict[str, Any]) -> ActionQueueItem:
    company_id = str(row["company_id"])
    record_id = str(row["id"])
    company_name = str(row.get("company_name") or "")
    confidence = row.get("confidence")
    if confidence is not None:
        confidence = float(confidence)
    return ActionQueueItem(
        item_key=_item_key(QUEUE_CATEGORY_STALE_EVIDENCE, company_id, record_id=record_id),
        priority_rank=PRIORITY_RANK[QUEUE_CATEGORY_STALE_EVIDENCE],
        category=QUEUE_CATEGORY_STALE_EVIDENCE,
        reason=_reason_stale_evidence(company_name, confidence),
        company_id=company_id,
        company_name=company_name,
        evidence_record_id=record_id,
        evidence_confidence=confidence,
        evidence_source_url=row.get("source_url"),
        pipeline_stage=row.get("pipeline_stage"),
        expected_value_cents=row.get("expected_value_cents"),
    )


def _sort_key(item: ActionQueueItem) -> tuple:
    """Sort by priority rank, then category-specific tie-breakers."""
    if item.category in (QUEUE_CATEGORY_OVERDUE, QUEUE_CATEGORY_DUE_TODAY):
        due = item.next_action_due_at or datetime.max.replace(tzinfo=timezone.utc)
        value = -(item.expected_value_cents or 0)
        return (item.priority_rank, due, value, item.company_name.lower())
    if item.category == QUEUE_CATEGORY_TIER_A:
        qualified = item.qualified_at or datetime.min.replace(tzinfo=timezone.utc)
        return (item.priority_rank, -qualified.timestamp(), item.company_name.lower())
    if item.category == QUEUE_CATEGORY_WARM_INTRO:
        return (item.priority_rank, item.company_name.lower(), item.contact_name or "")
    if item.category == QUEUE_CATEGORY_STALE_EVIDENCE:
        value = -(item.expected_value_cents or 0)
        return (item.priority_rank, value, item.company_name.lower())
    return (item.priority_rank, item.company_name.lower())


def prioritize_queue_items(items: list[ActionQueueItem], *, limit: int) -> tuple[ActionQueueItem, ...]:
    """Deduplicate by item_key (first wins), sort, and cap."""
    seen: set[str] = set()
    unique: list[ActionQueueItem] = []
    for item in sorted(items, key=_sort_key):
        if item.item_key in seen:
            continue
        seen.add(item.item_key)
        unique.append(item)
        if len(unique) >= limit:
            break
    return tuple(unique)


def load_action_queue(
    conn: psycopg.Connection,
    repo: ActionQueueRepository,
    *,
    now: datetime | None = None,
    limit: int = DEFAULT_QUEUE_LIMIT,
) -> ActionQueueData:
    """Load and prioritize the daily acquisition action queue."""
    reference = now or datetime.now(timezone.utc)
    day_start, day_end = _utc_day_bounds(reference)
    tier_a_since = reference - timedelta(days=TIER_A_RECENT_QUALIFIED_DAYS)

    raw_items: list[ActionQueueItem] = []
    raw_items.extend(
        _parse_overdue_row(row)
        for row in repo.list_overdue_next_actions(conn, reference=reference, limit=limit)
    )
    raw_items.extend(
        _parse_due_today_row(row)
        for row in repo.list_due_today_next_actions(
            conn, day_start=day_start, day_end=day_end, limit=limit
        )
    )
    raw_items.extend(
        _parse_tier_a_row(row)
        for row in repo.list_recently_qualified_tier_a(
            conn, since=tier_a_since, limit=limit
        )
    )
    raw_items.extend(
        _parse_warm_intro_row(row)
        for row in repo.list_warm_introduction_opportunities(conn, limit=limit)
    )
    raw_items.extend(
        _parse_stale_evidence_row(row)
        for row in repo.list_stale_high_value_evidence(
            conn,
            reference=reference,
            min_value_cents=HIGH_VALUE_MIN_CENTS,
            limit=limit,
        )
    )

    return ActionQueueData(
        items=prioritize_queue_items(raw_items, limit=limit),
        generated_at=reference,
    )
