"""Buying-group coverage and warm-introduction view logic (issue #124)."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal

from app.contacts import BUYING_ROLES, RELATIONSHIP_STRENGTHS
from app.research_records import is_public_evidence_type, validate_source_url

CoverageStatus = Literal["confirmed", "possible", "missing", "stale_employment"]

COVERAGE_SLOTS: tuple[tuple[str, str], ...] = (
    ("founder", "Founder"),
    ("technical_buyer", "CTO"),
    ("executive_buyer", "VP Engineering"),
    ("investor", "Investor"),
    ("introducer", "Introducer"),
)

_STALE_TITLE_RE = re.compile(
    r"\b(former|ex[-\s]|past|previously|alumni|retired)\b",
    re.IGNORECASE,
)
_DEPARTURE_BODY_RE = re.compile(
    r"\b(departed|left the company|no longer|stepped down|former role)\b",
    re.IGNORECASE,
)
_INVESTOR_ROLE_RE = re.compile(r"\binvestor\b", re.IGNORECASE)
_WARM_RELATIONSHIPS = frozenset({"warm", "strong", "champion"})
_OUTREACH_EMAIL_PERMISSIONS = frozenset({"permitted", "inferred"})

COVERAGE_STATUS_LABELS: dict[CoverageStatus, str] = {
    "confirmed": "Confirmed contact",
    "possible": "Possible contact",
    "missing": "Research gap",
    "stale_employment": "Stale employment",
}


@dataclass(frozen=True)
class CoverageContact:
    contact_id: str
    display_name: str
    title: str | None
    profile_url: str | None
    status: CoverageStatus
    status_note: str | None = None
    also_roles: tuple[str, ...] = ()


@dataclass(frozen=True)
class BuyingGroupSlot:
    role_key: str
    role_label: str
    entries: tuple[CoverageContact, ...] = ()
    slot_status: CoverageStatus = "missing"


@dataclass(frozen=True)
class WarmIntroPath:
    introducer_id: str
    introducer_name: str
    profile_url: str | None
    relationship_context: str
    interaction_metrics: str
    source_record_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class BuyingGroupView:
    slots: tuple[BuyingGroupSlot, ...]
    warm_intro_paths: tuple[WarmIntroPath, ...] = ()


def safe_profile_link(url: str | None, *, label: str | None = None) -> str:
    """Return a safe anchor for a contact profile URL, or escaped plain text."""
    if not url or not str(url).strip():
        return ""
    raw = str(url).strip()
    lower = raw.lower()
    if lower.startswith(("javascript:", "data:", "vbscript:", "file:")):
        return html.escape(label or raw)
    candidate = raw if "://" in raw else f"https://{raw}"
    try:
        validated = validate_source_url(candidate)
    except ValueError:
        return html.escape(raw)
    text = html.escape(label or validated)
    href = html.escape(validated, quote=True)
    return (
        f'<a class="buying-profile-link" href="{href}" '
        f'rel="noopener noreferrer" target="_blank">{text}</a>'
    )


def _contact_display_name(contact: dict[str, Any]) -> str:
    return str(
        contact.get("full_name")
        or contact.get("email")
        or contact.get("profile_url")
        or contact.get("id")
        or ""
    )


def _normalize_roles(contact: dict[str, Any]) -> list[str]:
    roles = contact.get("buying_roles") or []
    return [str(role) for role in roles if role]


def _record_contact_id(record: dict[str, Any]) -> str | None:
    contact_id = record.get("contact_id")
    if contact_id is None:
        return None
    return str(contact_id)


def _records_for_contact(
    records: list[dict[str, Any]], contact_id: str
) -> list[dict[str, Any]]:
    return [record for record in records if _record_contact_id(record) == contact_id]


def _company_wide_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [record for record in records if _record_contact_id(record) is None]


def _title_indicates_stale(title: str | None) -> bool:
    if not title:
        return False
    return bool(_STALE_TITLE_RE.search(title))


def _record_indicates_departure(record: dict[str, Any]) -> bool:
    body = str(record.get("body") or "")
    observed = str(record.get("observed_value") or "")
    combined = f"{body} {observed}"
    if not combined.strip():
        return False
    if _DEPARTURE_BODY_RE.search(combined):
        return True
    return _title_indicates_stale(observed)


def _has_role_evidence(
    contact: dict[str, Any],
    role_key: str,
    records: list[dict[str, Any]],
) -> bool:
    contact_id = str(contact.get("id", ""))
    relevant = _records_for_contact(records, contact_id) + _company_wide_records(records)
    for record in relevant:
        record_type = str(record.get("record_type", ""))
        if not is_public_evidence_type(record_type):
            continue
        haystack = " ".join(
            str(record.get(part) or "")
            for part in ("body", "observed_value", "source_name")
        )
        if role_key == "investor" and _INVESTOR_ROLE_RE.search(haystack):
            return True
        if role_key != "investor" and _contact_display_name(contact).lower() in haystack.lower():
            return True
        if role_key != "investor" and str(contact.get("title") or "").lower() in haystack.lower():
            return True
    return False


def _has_investor_evidence(
    contact: dict[str, Any],
    records: list[dict[str, Any]],
) -> bool:
    return _has_role_evidence(contact, "investor", records)


def _has_outreach_path(contact: dict[str, Any]) -> bool:
    email = contact.get("email")
    permission = str(contact.get("email_permission") or "")
    if email and permission in _OUTREACH_EMAIL_PERMISSIONS:
        return True
    strength = str(contact.get("relationship_strength") or "")
    return strength in _WARM_RELATIONSHIPS


def contact_coverage_status(
    contact: dict[str, Any],
    role_key: str,
    *,
    records: list[dict[str, Any]],
) -> tuple[CoverageStatus, str | None]:
    """Derive coverage status for one contact in one buying-role slot."""
    if contact.get("archived_at") is not None:
        return "stale_employment", "Contact archived — verify current role."

    contact_records = _records_for_contact(records, str(contact.get("id", "")))
    if _title_indicates_stale(str(contact.get("title") or "")):
        return "stale_employment", "Title indicates former employment."

    for record in contact_records:
        if _record_indicates_departure(record):
            return "stale_employment", "Research record indicates departure."

    if role_key == "investor":
        if _has_investor_evidence(contact, records):
            return "confirmed", "Investor linked via sourced evidence."
        return "possible", "Investor role without sourced evidence."

    if _has_outreach_path(contact) or _has_role_evidence(contact, role_key, records):
        return "confirmed", None

    return "possible", "Role assigned — outreach path not yet confirmed."


def _slot_status(entries: tuple[CoverageContact, ...]) -> CoverageStatus:
    if not entries:
        return "missing"
    priority: tuple[CoverageStatus, ...] = (
        "confirmed",
        "possible",
        "stale_employment",
        "missing",
    )
    for status in priority:
        if any(entry.status == status for entry in entries):
            return status
    return "missing"


def _format_interaction_metrics(contact: dict[str, Any]) -> str:
    parts: list[str] = []
    strength = contact.get("relationship_strength")
    if strength:
        label = RELATIONSHIP_STRENGTHS.get(str(strength), str(strength))
        parts.append(f"Relationship: {label}")
    last = contact.get("last_interaction_at")
    if last is not None:
        if isinstance(last, (datetime, date)):
            formatted = last.isoformat()
        else:
            formatted = str(last)
        parts.append(f"Last interaction: {formatted}")
    if not parts:
        return "No interaction metrics recorded."
    return " · ".join(parts)


def _relationship_context_for_introducer(
    contact_id: str,
    records: list[dict[str, Any]],
) -> tuple[str, tuple[str, ...]]:
    contexts: list[str] = []
    record_ids: list[str] = []
    for record in records:
        if str(record.get("record_type", "")) != "relationship_context":
            continue
        linked = _record_contact_id(record)
        if linked not in (None, contact_id):
            continue
        body = str(record.get("body") or "").strip()
        if body:
            contexts.append(body)
        record_id = record.get("id")
        if record_id is not None:
            record_ids.append(str(record_id))
    if not contexts:
        return "No explicit relationship context recorded.", ()
    return " ".join(contexts), tuple(record_ids)


def build_warm_intro_paths(
    contacts: list[dict[str, Any]],
    records: list[dict[str, Any]],
) -> tuple[WarmIntroPath, ...]:
    paths: list[WarmIntroPath] = []
    for contact in contacts:
        if contact.get("archived_at") is not None:
            continue
        roles = _normalize_roles(contact)
        if "introducer" not in roles:
            continue
        contact_id = str(contact.get("id", ""))
        context, record_ids = _relationship_context_for_introducer(contact_id, records)
        paths.append(
            WarmIntroPath(
                introducer_id=contact_id,
                introducer_name=_contact_display_name(contact),
                profile_url=contact.get("profile_url"),
                relationship_context=context,
                interaction_metrics=_format_interaction_metrics(contact),
                source_record_ids=record_ids,
            )
        )
    return tuple(paths)


def build_buying_group_view(
    contacts: list[dict[str, Any]],
    records: list[dict[str, Any]],
) -> BuyingGroupView:
    """Organize contacts into buying-role slots with coverage indicators."""
    active_contacts = [contact for contact in contacts if contact.get("archived_at") is None]
    archived_contacts = [contact for contact in contacts if contact.get("archived_at") is not None]
    ordered_contacts = active_contacts + archived_contacts

    slots: list[BuyingGroupSlot] = []
    for role_key, role_label in COVERAGE_SLOTS:
        entries: list[CoverageContact] = []
        for contact in ordered_contacts:
            roles = _normalize_roles(contact)
            if role_key not in roles:
                continue
            status, note = contact_coverage_status(
                contact, role_key, records=records
            )
            also_roles = tuple(
                other
                for other in roles
                if other != role_key and other in BUYING_ROLES
            )
            entries.append(
                CoverageContact(
                    contact_id=str(contact.get("id", "")),
                    display_name=_contact_display_name(contact),
                    title=contact.get("title"),
                    profile_url=contact.get("profile_url"),
                    status=status,
                    status_note=note,
                    also_roles=also_roles,
                )
            )
        entry_tuple = tuple(entries)
        slots.append(
            BuyingGroupSlot(
                role_key=role_key,
                role_label=role_label,
                entries=entry_tuple,
                slot_status=_slot_status(entry_tuple),
            )
        )

    return BuyingGroupView(
        slots=tuple(slots),
        warm_intro_paths=build_warm_intro_paths(contacts, records),
    )
