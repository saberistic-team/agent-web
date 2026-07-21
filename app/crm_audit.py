"""Bounded audit summaries and change detection for CRM lifecycle mutations."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

COMPANY_PRESENCE_FIELDS = frozenset({"website", "funding_summary", "notes"})
CONTACT_PRESENCE_FIELDS = frozenset({"profile_url", "email", "notes"})

COMPANY_PRESENCE_SUMMARY_KEYS = {
    "website": "has_website",
    "funding_summary": "has_funding_summary",
    "notes": "has_notes",
}
CONTACT_PRESENCE_SUMMARY_KEYS = {
    "profile_url": "has_profile_url",
    "email": "has_email",
    "notes": "has_notes",
}


def _iso_or_none(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _presence_flag(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return "[present]"


def _normalize_presence_value(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _presence_field_changed(before: Any, after: Any) -> bool:
    before_norm = _normalize_presence_value(before)
    after_norm = _normalize_presence_value(after)
    if not before_norm and not after_norm:
        return False
    if bool(before_norm) != bool(after_norm):
        return True
    return before_norm != after_norm


def changed_audit_summaries(
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    summarize: Callable[[dict[str, Any]], dict[str, Any]],
    presence_fields: frozenset[str] = frozenset(),
    presence_summary_keys: dict[str, str] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return before/after snapshots containing only fields that changed.

    When nothing changed, returns ``(None, None)`` so callers can skip no-op
    audit events consistently for companies and contacts.
    """
    before_summary = summarize(before)
    after_summary = summarize(after)
    summary_keys = presence_summary_keys or {}
    changed_keys: list[str] = []
    for key in sorted(set(before_summary) | set(after_summary)):
        raw_key = next((raw for raw, mapped in summary_keys.items() if mapped == key), key)
        if raw_key in presence_fields:
            if _presence_field_changed(before.get(raw_key), after.get(raw_key)):
                changed_keys.append(key)
            continue
        if before_summary.get(key) != after_summary.get(key):
            changed_keys.append(key)
    if not changed_keys:
        return None, None
    return (
        {key: before_summary[key] for key in changed_keys},
        {key: after_summary[key] for key in changed_keys},
    )


def company_archive_audit_summaries(
    existing: dict[str, Any],
    archived: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    archived_at = archived.get("archived_at")
    return (
        {"archived_at": None, "name": existing.get("name")},
        {
            "archived_at": _iso_or_none(archived_at),
            "name": archived.get("name"),
        },
    )


def company_restore_audit_summaries(
    archived: dict[str, Any],
    restored: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    return (
        {
            "archived_at": _iso_or_none(archived.get("archived_at")),
            "name": archived.get("name"),
        },
        {"archived_at": None, "name": restored.get("name")},
    )


def contact_archive_audit_summaries(
    existing: dict[str, Any],
    archived: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    archived_at = archived.get("archived_at")
    return (
        {"archived_at": None, "full_name": existing.get("full_name")},
        {
            "archived_at": _iso_or_none(archived_at),
            "full_name": archived.get("full_name"),
        },
    )


def contact_restore_audit_summaries(
    archived: dict[str, Any],
    restored: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    return (
        {
            "archived_at": _iso_or_none(archived.get("archived_at")),
            "full_name": archived.get("full_name"),
        },
        {"archived_at": None, "full_name": restored.get("full_name")},
    )


__all__ = [
    "_presence_flag",
    "changed_audit_summaries",
    "company_archive_audit_summaries",
    "company_restore_audit_summaries",
    "contact_archive_audit_summaries",
    "contact_restore_audit_summaries",
]
