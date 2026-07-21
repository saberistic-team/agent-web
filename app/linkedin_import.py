"""LinkedIn export import helpers — normalization, checksums, and snapshots."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Any

from app.contacts import normalize_contact_name, normalize_email, normalize_profile_url

LINKEDIN_IMPORT_SCHEMA_VERSION = "linkedin_export_v1"
SOURCE_TYPE_LINKEDIN = "linkedin"
SOURCE_KIND_CONNECTION = "linkedin_connection"
SOURCE_MANUAL = "manual"
SOURCE_LINKEDIN = "linkedin"

IMPORT_OUTCOMES = frozenset({"inserted", "updated", "unchanged", "skipped", "conflicted"})
PREVIEW_OUTCOMES = frozenset({"insert", "update", "unchanged", "conflict", "skipped"})
BATCH_STATUSES = frozenset({"committed", "failed", "rolled_back"})

_CONTACT_SNAPSHOT_FIELDS = (
    "full_name",
    "title",
    "profile_url",
    "email",
    "company_id",
    "archived_at",
    "field_sources",
)


def normalize_connection_row(row: dict[str, Any]) -> dict[str, Any]:
    """Return a normalized source identity without raw message bodies."""
    first = normalize_contact_name(str(row.get("first_name") or row.get("First Name") or ""))
    last = normalize_contact_name(str(row.get("last_name") or row.get("Last Name") or ""))
    full_name = normalize_contact_name(
        str(row.get("full_name") or row.get("name") or "")
    ) or " ".join(part for part in (first, last) if part).strip() or None
    title = normalize_contact_name(str(row.get("title") or row.get("Position") or ""))
    company_name = normalize_contact_name(
        str(row.get("company") or row.get("Company") or row.get("organization") or "")
    )
    connected_on = str(row.get("connected_on") or row.get("Connected On") or "").strip() or None
    profile_url: str | None = None
    raw_url = (
        row.get("profile_url")
        or row.get("url")
        or row.get("URL")
        or row.get("Profile URL")
    )
    if raw_url:
        try:
            profile_url = normalize_profile_url(str(raw_url))
        except ValueError:
            profile_url = None
    email: str | None = None
    raw_email = row.get("email") or row.get("Email") or row.get("Email Address")
    if raw_email:
        try:
            email = normalize_email(str(raw_email))
        except ValueError:
            email = None
    return {
        "source_kind": SOURCE_KIND_CONNECTION,
        "profile_url": profile_url,
        "full_name": full_name,
        "title": title,
        "company_name": company_name,
        "email": email,
        "connected_on": connected_on,
    }


def compute_import_checksum(connections: list[dict[str, Any]]) -> str:
    """Stable checksum for idempotent replay of the same normalized export."""
    identities = [normalize_connection_row(row) for row in connections]
    identities.sort(key=lambda item: (item.get("profile_url") or "", item.get("full_name") or ""))
    payload = {
        "schema_version": LINKEDIN_IMPORT_SCHEMA_VERSION,
        "connections": identities,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def empty_summary_counts() -> dict[str, int]:
    return {
        "inserted": 0,
        "updated": 0,
        "unchanged": 0,
        "skipped": 0,
        "conflicted": 0,
    }


def increment_summary(summary: dict[str, int], outcome: str) -> None:
    key = outcome
    if outcome == "insert":
        key = "inserted"
    elif outcome == "update":
        key = "updated"
    elif outcome == "conflict":
        key = "conflicted"
    if key in summary:
        summary[key] += 1


def snapshot_contact(contact: dict[str, Any]) -> dict[str, Any]:
    return {
        field: _json_safe(contact.get(field))
        for field in _CONTACT_SNAPSHOT_FIELDS
    }


def contact_matches_snapshot(contact: dict[str, Any], snapshot: dict[str, Any] | None) -> bool:
    if snapshot is None:
        return False
    for field in _CONTACT_SNAPSHOT_FIELDS:
        if str(contact.get(field) or "") != str(snapshot.get(field) or ""):
            return False
    return True


def contact_needs_update(contact: dict[str, Any], identity: dict[str, Any]) -> bool:
    desired_name = identity.get("full_name")
    desired_title = identity.get("title")
    current_name = contact.get("full_name")
    current_title = contact.get("title")
    if desired_name and desired_name != current_name:
        return True
    if desired_title and desired_title != current_title:
        return True
    return False


def parse_export_date(value: str | date | None) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d %b %Y"):
        try:
            from datetime import datetime

            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _json_safe(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, dict):
        return value
    return str(value)
