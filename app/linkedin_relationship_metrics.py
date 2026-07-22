"""Privacy-conscious relationship metrics from LinkedIn export metadata.

Derives connection and message participation signals without retaining raw
message bodies. Scoring inputs are explicit and deterministic.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

from app.contacts import normalize_contact_name
from app.linkedin_import import normalize_connection_row, parse_export_date

LINKEDIN_METRICS_SCHEMA_VERSION = "linkedin_metrics_v1"
MAX_STORED_MESSAGE_KEYS = 50_000

FORBIDDEN_MESSAGE_FIELDS = frozenset(
    {
        "content",
        "message",
        "body",
        "text",
        "subject",
    }
)


@dataclass(frozen=True)
class MessageMetadata:
    conversation_id: str
    from_name: str
    to_name: str
    sent_at: datetime | None
    message_key: str


@dataclass
class ContactMetricsAccumulator:
    profile_url: str | None = None
    connection_date: date | None = None
    conversation_ids: set[str] = field(default_factory=set)
    message_directions: dict[str, str] = field(default_factory=dict)
    message_timestamps: dict[str, datetime | None] = field(default_factory=dict)
    first_interaction_at: datetime | None = None
    last_interaction_at: datetime | None = None

    @property
    def inbound_count(self) -> int:
        return sum(1 for direction in self.message_directions.values() if direction == "inbound")

    @property
    def outbound_count(self) -> int:
        return sum(1 for direction in self.message_directions.values() if direction == "outbound")


def _normalize_name(value: str | None) -> str | None:
    return normalize_contact_name(value)


def _find_column(row: dict[str, str], *tokens: str) -> str:
    for key, value in row.items():
        lower = key.lower()
        if all(token in lower for token in tokens):
            return str(value or "").strip()
    return ""


def _reject_forbidden_fields(row: dict[str, Any]) -> None:
    for key, value in row.items():
        lower = key.lower()
        if lower in FORBIDDEN_MESSAGE_FIELDS or any(
            forbidden in lower for forbidden in FORBIDDEN_MESSAGE_FIELDS
        ):
            if str(value or "").strip():
                raise ValueError(f"Raw message field {key!r} must not be transmitted")


def parse_message_timestamp(value: str | date | datetime | None) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    for fmt in (
        "%Y-%m-%d",
        "%Y-%m-%d %H:%M:%S",
        "%m/%d/%Y",
        "%d %b %Y",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%SZ",
    ):
        try:
            parsed = datetime.strptime(text.replace(" UTC", "Z"), fmt)
            return parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    if text.endswith("Z"):
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return parsed.astimezone(timezone.utc)
        except ValueError:
            return None
    return None


def message_dedup_key(
    *,
    conversation_id: str,
    from_name: str,
    to_name: str,
    sent_at: datetime | None,
) -> str:
    payload = {
        "conversation_id": conversation_id,
        "from_name": _normalize_name(from_name) or "",
        "to_name": _normalize_name(to_name) or "",
        "sent_at": sent_at.isoformat() if sent_at else "",
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def normalize_message_row(row: dict[str, Any]) -> MessageMetadata | None:
    """Extract metadata from one messages.csv row; never retain body text."""
    if not isinstance(row, dict):
        return None
    if row.get("message_key") and row.get("conversation_id"):
        sent_at = parse_message_timestamp(row.get("sent_at"))
        return MessageMetadata(
            conversation_id=str(row["conversation_id"]).strip(),
            from_name=str(row.get("from_name") or "").strip(),
            to_name=str(row.get("to_name") or "").strip(),
            sent_at=sent_at,
            message_key=str(row["message_key"]).strip(),
        )
    _reject_forbidden_fields(row)
    conversation_id = _find_column(row, "conversation", "id") or _find_column(row, "conversation")
    if not conversation_id:
        return None
    from_name = _find_column(row, "from") or str(row.get("FROM") or row.get("From") or "").strip()
    to_name = _find_column(row, "to") or str(row.get("TO") or row.get("To") or "").strip()
    if not from_name and not to_name:
        return None
    sent_at = parse_message_timestamp(
        _find_column(row, "date") or str(row.get("DATE") or row.get("Date") or "").strip() or None
    )
    return MessageMetadata(
        conversation_id=conversation_id,
        from_name=from_name,
        to_name=to_name,
        sent_at=sent_at,
        message_key=message_dedup_key(
            conversation_id=conversation_id,
            from_name=from_name,
            to_name=to_name,
            sent_at=sent_at,
        ),
    )


def validate_message_rows_for_commit(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reject raw message bodies and ensure rows contain usable metadata."""
    validated: list[dict[str, Any]] = []
    for row in rows:
        if normalize_message_row(row) is None:
            continue
        validated.append(row)
    return validated


def build_connection_name_index(
    connections: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Map normalized connection full name -> identity metadata."""
    index: dict[str, dict[str, Any]] = {}
    for raw_row in connections:
        identity = normalize_connection_row(raw_row)
        name = _normalize_name(identity.get("full_name"))
        if not name:
            continue
        key = name.lower()
        if key not in index:
            index[key] = identity
    return index


def infer_account_owner_name(
    message_rows: list[dict[str, Any]],
    *,
    connection_names: set[str],
) -> str | None:
    """Guess export owner name as the most frequent non-connection participant."""
    counts: dict[str, int] = {}
    for row in message_rows:
        metadata = normalize_message_row(row)
        if metadata is None:
            continue
        for candidate in (metadata.from_name, metadata.to_name):
            normalized = _normalize_name(candidate)
            if not normalized:
                continue
            key = normalized.lower()
            if key in connection_names:
                continue
            counts[key] = counts.get(key, 0) + 1
    if not counts:
        return None
    winner = max(counts.items(), key=lambda item: (item[1], item[0]))[0]
    for row in message_rows:
        metadata = normalize_message_row(row)
        if metadata is None:
            continue
        for candidate in (metadata.from_name, metadata.to_name):
            normalized = _normalize_name(candidate)
            if normalized and normalized.lower() == winner:
                return normalized
    return winner.title()


def _touch_interaction(accumulator: ContactMetricsAccumulator, sent_at: datetime | None) -> None:
    if sent_at is None:
        return
    if accumulator.first_interaction_at is None or sent_at < accumulator.first_interaction_at:
        accumulator.first_interaction_at = sent_at
    if accumulator.last_interaction_at is None or sent_at > accumulator.last_interaction_at:
        accumulator.last_interaction_at = sent_at


def _record_message(
    accumulator: ContactMetricsAccumulator,
    metadata: MessageMetadata,
    *,
    direction: str,
) -> bool:
    if metadata.message_key in accumulator.message_directions:
        return False
    accumulator.message_directions[metadata.message_key] = direction
    accumulator.message_timestamps[metadata.message_key] = metadata.sent_at
    accumulator.conversation_ids.add(metadata.conversation_id)
    _touch_interaction(accumulator, metadata.sent_at)
    return True


def aggregate_messages_for_connections(
    message_rows: list[dict[str, Any]],
    connections: list[dict[str, Any]],
    *,
    owner_name: str | None = None,
) -> dict[str, ContactMetricsAccumulator]:
    """Aggregate per-contact metrics keyed by normalized profile URL."""
    name_index = build_connection_name_index(connections)
    connection_names = {name.lower() for name in name_index}
    resolved_owner = owner_name or infer_account_owner_name(
        message_rows,
        connection_names=connection_names,
    )
    owner_key = resolved_owner.lower() if resolved_owner else None

    by_profile: dict[str, ContactMetricsAccumulator] = {}
    for raw_row in connections:
        identity = normalize_connection_row(raw_row)
        profile_url = identity.get("profile_url")
        if not profile_url:
            continue
        accumulator = by_profile.setdefault(profile_url, ContactMetricsAccumulator(profile_url=profile_url))
        accumulator.connection_date = parse_export_date(identity.get("connected_on"))

    for row in message_rows:
        metadata = normalize_message_row(row)
        if metadata is None:
            continue
        from_key = (_normalize_name(metadata.from_name) or "").lower()
        to_key = (_normalize_name(metadata.to_name) or "").lower()

        contact_identity: dict[str, Any] | None = None
        direction: str | None = None

        if owner_key and from_key == owner_key and to_key in name_index:
            contact_identity = name_index[to_key]
            direction = "outbound"
        elif owner_key and to_key == owner_key and from_key in name_index:
            contact_identity = name_index[from_key]
            direction = "inbound"
        elif from_key in name_index and to_key not in connection_names:
            contact_identity = name_index[from_key]
            direction = "outbound"
        elif to_key in name_index and from_key not in connection_names:
            contact_identity = name_index[to_key]
            direction = "inbound"

        if contact_identity is None or direction is None:
            continue

        profile_url = contact_identity.get("profile_url")
        if not profile_url:
            continue
        accumulator = by_profile.setdefault(profile_url, ContactMetricsAccumulator(profile_url=profile_url))
        if accumulator.connection_date is None:
            accumulator.connection_date = parse_export_date(contact_identity.get("connected_on"))
        _record_message(accumulator, metadata, direction=direction)

    return by_profile


def recent_interaction_indicators(
    last_interaction_at: datetime | date | None,
    *,
    reference: datetime | date | None = None,
) -> dict[str, bool]:
    if last_interaction_at is None:
        return {"recent_30d": False, "recent_90d": False, "recent_180d": False}
    ref = reference or datetime.now(timezone.utc)
    if isinstance(last_interaction_at, datetime):
        last_date = last_interaction_at.date()
    else:
        last_date = last_interaction_at
    if isinstance(ref, datetime):
        ref_date = ref.date()
    else:
        ref_date = ref
    days = (ref_date - last_date).days
    return {
        "recent_30d": days <= 30,
        "recent_90d": days <= 90,
        "recent_180d": days <= 180,
    }


def compute_score_inputs(
    metrics: dict[str, Any],
    *,
    former_colleague: bool = False,
    warm_introducer: bool = False,
    reference: datetime | date | None = None,
) -> dict[str, Any]:
    """Visible, deterministic inputs for relationship scoring."""
    ref = reference or datetime.now(timezone.utc)
    ref_date = ref.date() if isinstance(ref, datetime) else ref
    connection_date_raw = metrics.get("connection_date")
    connection_date = parse_export_date(connection_date_raw)
    connection_tenure_days = (
        (ref_date - connection_date).days if connection_date is not None else None
    )
    last_raw = metrics.get("last_interaction_at")
    last_dt = parse_message_timestamp(last_raw)
    days_since_last = None
    if last_dt is not None:
        days_since_last = (ref_date - last_dt.date()).days
    recency = recent_interaction_indicators(last_dt, reference=ref)
    inbound = int(metrics.get("inbound_count") or 0)
    outbound = int(metrics.get("outbound_count") or 0)
    return {
        "connection_tenure_days": connection_tenure_days,
        "conversation_count": int(metrics.get("conversation_count") or 0),
        "inbound_count": inbound,
        "outbound_count": outbound,
        "two_way": bool(inbound > 0 and outbound > 0),
        "days_since_last_interaction": days_since_last,
        "recent_30d": recency["recent_30d"],
        "recent_90d": recency["recent_90d"],
        "recent_180d": recency["recent_180d"],
        "former_colleague": bool(former_colleague),
        "warm_introducer": bool(warm_introducer),
    }


def compute_relationship_score(score_inputs: dict[str, Any]) -> int:
    """Deterministic score from visible inputs only (no message text)."""
    score = 0
    score += min(int(score_inputs.get("conversation_count") or 0) * 5, 25)
    participation = int(score_inputs.get("inbound_count") or 0) + int(
        score_inputs.get("outbound_count") or 0
    )
    score += min(participation, 20)
    if score_inputs.get("two_way"):
        score += 15
    if score_inputs.get("recent_90d"):
        score += 10
    elif score_inputs.get("recent_180d"):
        score += 5
    if score_inputs.get("former_colleague"):
        score += 10
    if score_inputs.get("warm_introducer"):
        score += 15
    tenure = score_inputs.get("connection_tenure_days")
    if isinstance(tenure, int) and tenure >= 0:
        score += min(tenure // 30, 15)
    return min(score, 100)


def _iso(value: datetime | date | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return value.isoformat()


def metrics_from_accumulator(accumulator: ContactMetricsAccumulator) -> dict[str, Any]:
    return {
        "connection_date": _iso(accumulator.connection_date),
        "conversation_count": len(accumulator.conversation_ids),
        "inbound_count": accumulator.inbound_count,
        "outbound_count": accumulator.outbound_count,
        "first_interaction_at": _iso(accumulator.first_interaction_at),
        "last_interaction_at": _iso(accumulator.last_interaction_at),
        "two_way": accumulator.inbound_count > 0 and accumulator.outbound_count > 0,
        "message_directions": dict(accumulator.message_directions),
    }


def parse_stored_metrics(raw: Any) -> dict[str, Any]:
    if not raw or not isinstance(raw, dict):
        return {}
    return dict(raw)


def _direction_map(raw: dict[str, Any]) -> dict[str, str]:
    directions = raw.get("message_directions")
    if isinstance(directions, dict):
        return {str(key): str(value) for key, value in directions.items()}
    legacy_keys = raw.get("message_keys") or []
    if isinstance(legacy_keys, list):
        return {str(key): "unknown" for key in legacy_keys}
    return {}


def merge_stored_metrics(
    existing: dict[str, Any] | None,
    incoming: dict[str, Any],
    *,
    former_colleague: bool = False,
    warm_introducer: bool = False,
    reference: datetime | None = None,
) -> dict[str, Any]:
    """Incrementally merge metrics without double-counting message keys."""
    ref = reference or datetime.now(timezone.utc)
    base = parse_stored_metrics(existing)
    merged_directions = _direction_map(base)
    merged_directions.update(_direction_map(incoming))
    if len(merged_directions) > MAX_STORED_MESSAGE_KEYS:
        merged_directions = dict(
            list(sorted(merged_directions.items()))[:MAX_STORED_MESSAGE_KEYS]
        )

    inbound = sum(1 for direction in merged_directions.values() if direction == "inbound")
    outbound = sum(1 for direction in merged_directions.values() if direction == "outbound")
    conversation_count = max(
        int(base.get("conversation_count") or 0),
        int(incoming.get("conversation_count") or 0),
        len(merged_directions),
    )

    first_candidates = [
        parse_message_timestamp(base.get("first_interaction_at")),
        parse_message_timestamp(incoming.get("first_interaction_at")),
    ]
    first_candidates = [item for item in first_candidates if item is not None]
    first_interaction = min(first_candidates) if first_candidates else None

    last_candidates = [
        parse_message_timestamp(base.get("last_interaction_at")),
        parse_message_timestamp(incoming.get("last_interaction_at")),
    ]
    last_candidates = [item for item in last_candidates if item is not None]
    last_interaction = max(last_candidates) if last_candidates else None

    connection_date = parse_export_date(incoming.get("connection_date")) or parse_export_date(
        base.get("connection_date")
    )

    metrics = {
        "schema_version": LINKEDIN_METRICS_SCHEMA_VERSION,
        "connection_date": _iso(connection_date),
        "conversation_count": conversation_count,
        "inbound_count": inbound,
        "outbound_count": outbound,
        "first_interaction_at": _iso(first_interaction),
        "last_interaction_at": _iso(last_interaction),
        "two_way": inbound > 0 and outbound > 0,
        "message_directions": merged_directions,
        "updated_at": ref.isoformat(),
    }
    recency = recent_interaction_indicators(last_interaction, reference=ref)
    metrics.update(recency)
    score_inputs = compute_score_inputs(
        metrics,
        former_colleague=former_colleague,
        warm_introducer=warm_introducer,
        reference=ref,
    )
    metrics["score_inputs"] = score_inputs
    metrics["computed_score"] = compute_relationship_score(score_inputs)
    return metrics


def build_metrics_for_contact(
    accumulator: ContactMetricsAccumulator,
    *,
    existing: dict[str, Any] | None = None,
    former_colleague: bool = False,
    warm_introducer: bool = False,
    reference: datetime | None = None,
) -> dict[str, Any]:
    incoming = metrics_from_accumulator(accumulator)
    return merge_stored_metrics(
        existing,
        incoming,
        former_colleague=former_colleague,
        warm_introducer=warm_introducer,
        reference=reference,
    )


def finalize_stored_metrics(
    metrics: dict[str, Any],
    *,
    former_colleague: bool = False,
    warm_introducer: bool = False,
    reference: datetime | None = None,
) -> dict[str, Any]:
    """Recompute score inputs when CRM context flags change."""
    ref = reference or datetime.now(timezone.utc)
    payload = dict(metrics)
    payload["schema_version"] = LINKEDIN_METRICS_SCHEMA_VERSION
    last_interaction = parse_message_timestamp(payload.get("last_interaction_at"))
    payload.update(recent_interaction_indicators(last_interaction, reference=ref))
    score_inputs = compute_score_inputs(
        payload,
        former_colleague=former_colleague,
        warm_introducer=warm_introducer,
        reference=ref,
    )
    payload["score_inputs"] = score_inputs
    payload["computed_score"] = compute_relationship_score(score_inputs)
    payload["updated_at"] = ref.isoformat()
    return payload
