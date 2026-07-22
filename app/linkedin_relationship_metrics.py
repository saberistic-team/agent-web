"""Privacy-conscious relationship metrics from LinkedIn export metadata.

Derives connection and message participation signals without retaining raw
message bodies. Message rows may include body/subject columns in exports; this
module strips them and never persists that text.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from app.contacts import normalize_contact_name
from app.linkedin_import import parse_export_date

RELATIONSHIP_METRICS_SCHEMA_VERSION = "linkedin_relationship_v1"
MAX_STORED_MESSAGE_KEYS = 10_000

FORBIDDEN_MESSAGE_FIELDS = frozenset(
    {"content", "body", "subject", "message", "text", "html", "snippet"}
)

RECENT_INTERACTION_WINDOWS_DAYS = (30, 90)


def _normalize_header_key(key: str) -> str:
    return key.strip().lower().replace("_", " ")


def _field_value(row: dict[str, Any], *candidates: str) -> str | None:
    normalized = {_normalize_header_key(str(k)): v for k, v in row.items()}
    for candidate in candidates:
        value = normalized.get(_normalize_header_key(candidate))
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _conversation_id(row: dict[str, Any]) -> str | None:
    for key, value in row.items():
        lower = _normalize_header_key(str(key))
        if "conversation" in lower and "id" in lower:
            text = str(value).strip()
            if text:
                return text
    return None


def normalize_message_metadata(row: dict[str, Any]) -> dict[str, Any] | None:
    """Return safe message metadata; drop bodies and empty rows."""
    if not any(str(value or "").strip() for value in row.values()):
        return None
    conversation_id = _conversation_id(row)
    sent_raw = _field_value(row, "date", "sent at", "sent")
    if not conversation_id or not sent_raw:
        return None
    sent_at = parse_message_timestamp(sent_raw)
    if sent_at is None:
        return None
    sender = normalize_contact_name(_field_value(row, "from", "sender"))
    recipient = normalize_contact_name(_field_value(row, "to", "recipient"))
    if not sender and not recipient:
        return None
    folder = _field_value(row, "folder")
    return {
        "conversation_id": conversation_id,
        "sent_at": sent_at.isoformat(),
        "from": sender,
        "to": recipient,
        "folder": folder,
    }


def parse_message_timestamp(value: str | date | datetime | None) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    if "T" in text:
        try:
            normalized = text.replace("Z", "+00:00")
            return datetime.fromisoformat(normalized).date()
        except ValueError:
            pass
    parsed = parse_export_date(text)
    if parsed is not None:
        return parsed
    for fmt in ("%Y-%m-%d %H:%M:%S", "%m/%d/%Y %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def message_identity(meta: dict[str, Any]) -> str:
    return "|".join(
        [
            str(meta.get("conversation_id") or ""),
            str(meta.get("sent_at") or ""),
            str(meta.get("from") or ""),
            str(meta.get("to") or ""),
        ]
    )


def _names_match(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    return left.strip().lower() == right.strip().lower()


def message_involves_contact(
    meta: dict[str, Any],
    *,
    contact_name: str,
    owner_name: str,
) -> bool:
    """True when the row is a direct owner↔contact exchange."""
    contact = normalize_contact_name(contact_name)
    owner = normalize_contact_name(owner_name)
    if not contact or not owner:
        return False
    sender = meta.get("from")
    recipient = meta.get("to")
    owner_to_contact = _names_match(sender, owner) and _names_match(recipient, contact)
    contact_to_owner = _names_match(sender, contact) and _names_match(recipient, owner)
    return owner_to_contact or contact_to_owner


def message_direction(
    meta: dict[str, Any],
    *,
    contact_name: str,
    owner_name: str,
) -> str | None:
    contact = normalize_contact_name(contact_name)
    owner = normalize_contact_name(owner_name)
    if not contact or not owner:
        return None
    sender = meta.get("from")
    recipient = meta.get("to")
    if _names_match(sender, owner) and _names_match(recipient, contact):
        return "outbound"
    if _names_match(sender, contact) and _names_match(recipient, owner):
        return "inbound"
    return None


def _recent_flags(last_interaction: date | None, reference_date: date) -> tuple[bool, bool]:
    if last_interaction is None:
        return False, False
    delta = reference_date - last_interaction
    recent_30d = delta.days <= RECENT_INTERACTION_WINDOWS_DAYS[0]
    recent_90d = delta.days <= RECENT_INTERACTION_WINDOWS_DAYS[1]
    return recent_30d, recent_90d


def relationship_scoring_inputs(metrics: dict[str, Any]) -> dict[str, Any]:
    """Deterministic, visible inputs used for relationship scoring."""
    return {
        "schema_version": metrics.get("schema_version", RELATIONSHIP_METRICS_SCHEMA_VERSION),
        "connection_date": metrics.get("connection_date"),
        "conversation_count": int(metrics.get("conversation_count") or 0),
        "message_count": int(metrics.get("message_count") or 0),
        "inbound_count": int(metrics.get("inbound_count") or 0),
        "outbound_count": int(metrics.get("outbound_count") or 0),
        "first_interaction_at": metrics.get("first_interaction_at"),
        "last_interaction_at": metrics.get("last_interaction_at"),
        "recent_interaction_30d": bool(metrics.get("recent_interaction_30d")),
        "recent_interaction_90d": bool(metrics.get("recent_interaction_90d")),
        "two_way_conversation": bool(metrics.get("two_way_conversation")),
    }


def empty_relationship_metrics(*, reference_date: date | None = None) -> dict[str, Any]:
    ref = reference_date or datetime.now(timezone.utc).date()
    base = {
        "schema_version": RELATIONSHIP_METRICS_SCHEMA_VERSION,
        "connection_date": None,
        "conversation_count": 0,
        "message_count": 0,
        "inbound_count": 0,
        "outbound_count": 0,
        "first_interaction_at": None,
        "last_interaction_at": None,
        "recent_interaction_30d": False,
        "recent_interaction_90d": False,
        "two_way_conversation": False,
        "message_keys": [],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    base["scoring_inputs"] = relationship_scoring_inputs(base)
    base["reference_date"] = ref.isoformat()
    return base


def merge_relationship_metrics(
    existing: dict[str, Any] | None,
    *,
    contact_name: str,
    owner_name: str,
    connection_date: date | None,
    message_rows: list[dict[str, Any]],
    reference_date: date | None = None,
) -> dict[str, Any]:
    """Incrementally merge export message metadata for one contact."""
    ref = reference_date or datetime.now(timezone.utc).date()
    state = empty_relationship_metrics(reference_date=ref)
    if existing:
        state.update(existing)
        state["message_keys"] = list(existing.get("message_keys") or [])

    known_keys = set(state["message_keys"])
    inbound = int(state.get("inbound_count") or 0)
    outbound = int(state.get("outbound_count") or 0)
    message_count = int(state.get("message_count") or 0)
    conversations: set[str] = set()
    for key in state["message_keys"]:
        conv = key.split("|", 1)[0]
        if conv:
            conversations.add(conv)

    first = parse_message_timestamp(state.get("first_interaction_at"))
    last = parse_message_timestamp(state.get("last_interaction_at"))

    if connection_date is not None:
        state["connection_date"] = connection_date.isoformat()
        first = connection_date if first is None else min(first, connection_date)
        last = connection_date if last is None else max(last, connection_date)

    for raw_row in message_rows:
        meta = normalize_message_metadata(raw_row)
        if meta is None:
            continue
        if not message_involves_contact(meta, contact_name=contact_name, owner_name=owner_name):
            continue
        key = message_identity(meta)
        if key in known_keys:
            continue
        direction = message_direction(
            meta,
            contact_name=contact_name,
            owner_name=owner_name,
        )
        if direction is None:
            continue
        known_keys.add(key)
        state["message_keys"].append(key)
        message_count += 1
        conversations.add(str(meta["conversation_id"]))
        sent_at = parse_message_timestamp(meta["sent_at"])
        if sent_at is not None:
            first = sent_at if first is None else min(first, sent_at)
            last = sent_at if last is None else max(last, sent_at)
        if direction == "inbound":
            inbound += 1
        else:
            outbound += 1

    if len(state["message_keys"]) > MAX_STORED_MESSAGE_KEYS:
        state["message_keys"] = sorted(state["message_keys"])[-MAX_STORED_MESSAGE_KEYS:]

    state["conversation_count"] = len(conversations)
    state["message_count"] = message_count
    state["inbound_count"] = inbound
    state["outbound_count"] = outbound
    state["first_interaction_at"] = first.isoformat() if first else None
    state["last_interaction_at"] = last.isoformat() if last else None
    recent_30d, recent_90d = _recent_flags(last, ref)
    state["recent_interaction_30d"] = recent_30d
    state["recent_interaction_90d"] = recent_90d
    state["two_way_conversation"] = inbound > 0 and outbound > 0
    state["reference_date"] = ref.isoformat()
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    state["scoring_inputs"] = relationship_scoring_inputs(state)
    return state


def strip_message_bodies(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove private message text fields before transmission or storage."""
    cleaned: list[dict[str, Any]] = []
    for row in rows:
        safe: dict[str, Any] = {}
        for key, value in row.items():
            if _normalize_header_key(str(key)) in FORBIDDEN_MESSAGE_FIELDS:
                continue
            safe[key] = value
        cleaned.append(safe)
    return cleaned


def assert_no_message_bodies(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        for key, value in row.items():
            if _normalize_header_key(str(key)) in FORBIDDEN_MESSAGE_FIELDS and str(value or "").strip():
                raise ValueError("message bodies must not be transmitted in the default flow")


def connection_date_for_identity(
    identity: dict[str, Any],
    *,
    connection_by_profile: dict[str, date | None],
) -> date | None:
    profile_url = identity.get("profile_url")
    if profile_url and profile_url in connection_by_profile:
        return connection_by_profile[profile_url]
    connected_on = identity.get("connected_on")
    return parse_export_date(connected_on)


def build_connection_date_index(connections: list[dict[str, Any]]) -> dict[str, date | None]:
    from app.linkedin_import import normalize_connection_row

    index: dict[str, date | None] = {}
    for row in connections:
        identity = normalize_connection_row(row)
        profile_url = identity.get("profile_url")
        if not profile_url:
            continue
        index[profile_url] = parse_export_date(identity.get("connected_on"))
    return index


def metrics_last_interaction_date(metrics: dict[str, Any] | None) -> date | None:
    if not metrics:
        return None
    return parse_message_timestamp(metrics.get("last_interaction_at"))
