"""Contact buying roles, validation, and duplicate detection helpers."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

BUYING_ROLES: frozenset[str] = frozenset(
    {
        "founder",
        "technical_buyer",
        "executive_buyer",
        "influencer",
        "investor",
        "introducer",
        "other",
    }
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

RELATIONSHIP_STRENGTHS: frozenset[str] = frozenset(
    {"unknown", "weak", "moderate", "strong", "champion"}
)

RELATIONSHIP_STRENGTH_LABELS: dict[str, str] = {
    "unknown": "Unknown",
    "weak": "Weak",
    "moderate": "Moderate",
    "strong": "Strong",
    "champion": "Champion",
}

EMAIL_PERMISSIONS: frozenset[str] = frozenset(
    {"unknown", "implied", "explicit", "do_not_contact"}
)

EMAIL_PERMISSION_LABELS: dict[str, str] = {
    "unknown": "Unknown",
    "implied": "Implied",
    "explicit": "Explicit",
    "do_not_contact": "Do not contact",
}

_UNSAFE_URL_SCHEMES = frozenset({"javascript", "data", "vbscript", "file"})


def normalize_email(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip().lower()
    return stripped or None


def normalize_name(value: str | None) -> str | None:
    if value is None:
        return None
    collapsed = re.sub(r"\s+", " ", value.strip().lower())
    return collapsed or None


def normalize_profile_url(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    parsed = urlparse(stripped)
    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https"}:
        raise ValueError("profile URL must use http or https")
    if scheme in _UNSAFE_URL_SCHEMES:
        raise ValueError("profile URL scheme is not allowed")
    if not parsed.netloc:
        raise ValueError("profile URL must include a host")
    host = parsed.netloc.lower().removeprefix("www.")
    path = parsed.path.rstrip("/") or "/"
    query = f"?{parsed.query}" if parsed.query else ""
    return f"{scheme}://{host}{path}{query}".lower()


def validate_buying_roles(roles: list[str]) -> list[str]:
    normalized: list[str] = []
    for role in roles:
        stripped = role.strip()
        if not stripped:
            continue
        if stripped not in BUYING_ROLES:
            allowed = ", ".join(sorted(BUYING_ROLES))
            raise ValueError(f"buying role must be one of: {allowed}")
        if stripped not in normalized:
            normalized.append(stripped)
    return normalized


def format_buying_roles(roles: list[str]) -> str:
    if not roles:
        return ""
    labels = [BUYING_ROLE_LABELS.get(role, role) for role in sorted(roles)]
    return ", ".join(labels)


def render_buying_role_badges(roles: list[str]) -> str:
    if not roles:
        return '<span class="admin-note">No roles</span>'
    badges = []
    for role in sorted(roles):
        label = BUYING_ROLE_LABELS.get(role, role)
        badges.append(
            f'<span class="contact-role-badge contact-role-badge--{html.escape(role, quote=True)}">'
            f"{html.escape(label)}</span>"
        )
    return " ".join(badges)


def contact_display_name(contact: dict[str, Any]) -> str:
    full_name = str(contact.get("full_name") or "").strip()
    if full_name:
        return full_name
    email = str(contact.get("email") or "").strip()
    if email:
        return email
    profile_url = str(contact.get("profile_url") or "").strip()
    if profile_url:
        return profile_url
    return str(contact.get("id", "Contact"))


def parse_optional_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    normalized = stripped.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def format_contact_timestamp(value: datetime | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        parsed = parse_optional_datetime(value)
        if parsed is None:
            return html.escape(value)
        value = parsed
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return html.escape(value.strftime("%Y-%m-%d %H:%M UTC"))


@dataclass(frozen=True)
class ContactDuplicateMatch:
    contact_id: UUID
    reason: str
    contact: dict[str, Any]


class ContactFormData(BaseModel):
    full_name: str = Field(..., min_length=1, max_length=500)
    title: str | None = Field(default=None, max_length=500)
    profile_url: str | None = Field(default=None, max_length=2000)
    email: str | None = Field(default=None, max_length=500)
    email_provenance: str | None = Field(default=None, max_length=1000)
    email_permission: str | None = Field(default=None, max_length=64)
    company_id: str | None = Field(default=None, max_length=64)
    last_interaction_at: str | None = Field(default=None, max_length=64)
    relationship_strength: str | None = Field(default=None, max_length=64)
    notes: str | None = Field(default=None, max_length=10_000)
    buying_roles: list[str] = Field(default_factory=list)
    confirm_duplicates: bool = False

    @field_validator("full_name", "title", "email_provenance", "notes")
    @classmethod
    def strip_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @field_validator("email")
    @classmethod
    def normalize_email_field(cls, value: str | None) -> str | None:
        return normalize_email(value)

    @field_validator("profile_url")
    @classmethod
    def validate_profile_url_field(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            return None
        return normalize_profile_url(stripped)

    @field_validator("email_permission", "relationship_strength")
    @classmethod
    def validate_optional_enums(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @field_validator("buying_roles", mode="before")
    @classmethod
    def coerce_roles(cls, value: object) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value] if value.strip() else []
        if isinstance(value, (list, tuple)):
            return [str(item) for item in value]
        return []

    @model_validator(mode="after")
    def validate_roles_and_enums(self) -> ContactFormData:
        self.buying_roles = validate_buying_roles(self.buying_roles)
        if self.email_permission and self.email_permission not in EMAIL_PERMISSIONS:
            allowed = ", ".join(sorted(EMAIL_PERMISSIONS))
            raise ValueError(f"email_permission must be one of: {allowed}")
        if self.relationship_strength and self.relationship_strength not in RELATIONSHIP_STRENGTHS:
            allowed = ", ".join(sorted(RELATIONSHIP_STRENGTHS))
            raise ValueError(f"relationship_strength must be one of: {allowed}")
        return self

    def parsed_company_id(self) -> UUID | None:
        if not self.company_id or not self.company_id.strip():
            return None
        return UUID(self.company_id.strip())

    def parsed_last_interaction_at(self) -> datetime | None:
        return parse_optional_datetime(self.last_interaction_at)
