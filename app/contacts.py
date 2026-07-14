"""Contact normalization, buying roles, and duplicate detection."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

BUYING_ROLES: tuple[str, ...] = (
    "founder",
    "technical_buyer",
    "executive_buyer",
    "influencer",
    "investor",
    "introducer",
    "other",
)

BUYING_ROLE_LABELS: dict[str, str] = {
    "founder": "Founder",
    "technical_buyer": "Technical buyer",
    "executive_buyer": "Executive buyer",
    "influencer": "Influencer",
    "investor": "Investor",
    "introducer": "Introducer",
    "other": "Other",
}

RELATIONSHIP_STRENGTHS: tuple[str, ...] = ("weak", "fair", "good", "strong")

RELATIONSHIP_STRENGTH_LABELS: dict[str, str] = {
    "weak": "Weak",
    "fair": "Fair",
    "good": "Good",
    "strong": "Strong",
}

EMAIL_PERMISSIONS: tuple[str, ...] = ("permitted", "do_not_contact", "unknown")

EMAIL_PERMISSION_LABELS: dict[str, str] = {
    "permitted": "Permitted",
    "do_not_contact": "Do not contact",
    "unknown": "Unknown",
}

_LINKEDIN_PROFILE_RE = re.compile(
    r"(?:https?://)?(?:www\.)?linkedin\.com/in/([a-zA-Z0-9\-_%]+)/?",
    re.IGNORECASE,
)


def normalize_profile_url(url: str | None) -> str | None:
    """Normalize a LinkedIn or profile URL for duplicate detection."""
    if not url:
        return None
    trimmed = url.strip()
    if not trimmed:
        return None
    match = _LINKEDIN_PROFILE_RE.search(trimmed)
    if match:
        slug = match.group(1).lower().rstrip("/")
        return f"linkedin.com/in/{slug}"
    parsed = urlparse(trimmed if "://" in trimmed else f"https://{trimmed}")
    host = (parsed.netloc or parsed.path).lower().removeprefix("www.")
    path = parsed.path.lower().rstrip("/") if parsed.netloc else ""
    if not host:
        return None
    return f"{host}{path}" if path else host


def normalize_email(email: str | None) -> str | None:
    if not email:
        return None
    trimmed = email.strip().lower()
    return trimmed or None


def normalize_name(name: str | None) -> str | None:
    if not name:
        return None
    trimmed = " ".join(name.strip().split()).lower()
    return trimmed or None


def duplicate_warnings(
    *,
    matches: dict[str, list[dict[str, Any]]],
) -> list[str]:
    """Build human-readable duplicate warnings from repository match groups."""
    warnings: list[str] = []
    for profile_matches in matches.get("profile_url", []):
        name = profile_matches.get("name") or profile_matches.get("full_name") or "Unknown"
        warnings.append(f"Profile URL matches existing contact: {name}")
    for email_matches in matches.get("email", []):
        name = email_matches.get("name") or email_matches.get("full_name") or "Unknown"
        warnings.append(f"Email matches existing contact: {name}")
    for name_matches in matches.get("name_company", []):
        name = name_matches.get("name") or name_matches.get("full_name") or "Unknown"
        warnings.append(f"Name and company match existing contact: {name}")
    return warnings


def parse_buying_roles(selected: list[str]) -> list[str]:
    """Return validated buying roles preserving order."""
    seen: set[str] = set()
    roles: list[str] = []
    for role in selected:
        if role in BUYING_ROLES and role not in seen:
            seen.add(role)
            roles.append(role)
    return roles


def contact_display_name(contact: dict[str, Any]) -> str:
    return str(contact.get("name") or contact.get("full_name") or contact.get("email") or "Contact")


def format_contact_id(contact_id: UUID | str) -> str:
    return str(contact_id)
