"""Contact field registries, validation, and duplicate helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

BUYING_ROLES: dict[str, str] = {
    "founder": "Founder",
    "technical_buyer": "Technical buyer",
    "executive_buyer": "Executive buyer",
    "influencer": "Influencer",
    "investor": "Investor",
    "introducer": "Introducer",
    "other": "Other",
}
RELATIONSHIP_STRENGTHS: dict[str, str] = {
    "weak": "Weak",
    "moderate": "Moderate",
    "strong": "Strong",
    "unknown": "Unknown",
}
EMAIL_PROVENANCES: dict[str, str] = {
    "linkedin": "LinkedIn",
    "introducer": "Introducer",
    "conference": "Conference",
    "manual": "Manual entry",
    "import": "Import",
    "other": "Other",
}

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_contact_name(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = _WHITESPACE_RE.sub(" ", value.strip())
    return normalized or None


def normalize_email(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    return normalized or None


def normalize_profile_url(value: str | None) -> str | None:
    """Return a comparable profile URL, or None for an intentionally empty field."""
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    parsed = urlparse(text if "://" in text else f"https://{text}", scheme="https")
    if not parsed.hostname:
        raise ValueError("profile URL must be valid")
    host = parsed.hostname.rstrip(".").lower()
    if host.startswith("www."):
        host = host[4:]
    path = parsed.path.rstrip("/") or ""
    scheme = (parsed.scheme or "https").lower()
    return f"{scheme}://{host}{path}".lower()


def _validate_registry(value: str | None, registry: dict[str, str], field: str) -> str | None:
    if value is None or not value.strip():
        return None
    if value not in registry:
        raise ValueError(f"unknown {field}: {value}")
    return value


def _validate_buying_roles(values: list[str] | None) -> list[str]:
    if not values:
        return []
    seen: set[str] = set()
    normalized: list[str] = []
    for value in values:
        if not value or not value.strip():
            continue
        if value not in BUYING_ROLES:
            raise ValueError(f"unknown buying role: {value}")
        if value not in seen:
            seen.add(value)
            normalized.append(value)
    return normalized


class ContactCreate(BaseModel):
    full_name: str = Field(min_length=1, max_length=500)
    title: str | None = Field(default=None, max_length=500)
    profile_url: str | None = Field(default=None, max_length=2000)
    email: str | None = Field(default=None, max_length=320)
    email_permitted: bool = False
    email_provenance: str | None = None
    company_id: UUID | None = None
    last_interaction_at: date | None = None
    relationship_strength: str | None = None
    notes: str | None = Field(default=None, max_length=10000)
    buying_roles: list[str] = Field(default_factory=list)

    @field_validator("full_name")
    @classmethod
    def validate_full_name(cls, value: str) -> str:
        normalized = normalize_contact_name(value)
        if normalized is None:
            raise ValueError("name must not be empty")
        return normalized

    @field_validator("profile_url")
    @classmethod
    def validate_profile_url(cls, value: str | None) -> str | None:
        return normalize_profile_url(value)

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str | None) -> str | None:
        return normalize_email(value)

    @field_validator("email_provenance")
    @classmethod
    def validate_email_provenance(cls, value: str | None) -> str | None:
        return _validate_registry(value, EMAIL_PROVENANCES, "email provenance")

    @field_validator("relationship_strength")
    @classmethod
    def validate_relationship_strength(cls, value: str | None) -> str | None:
        return _validate_registry(value, RELATIONSHIP_STRENGTHS, "relationship strength")

    @field_validator("buying_roles")
    @classmethod
    def validate_buying_roles(cls, value: list[str] | None) -> list[str]:
        return _validate_buying_roles(value)


class ContactUpdate(ContactCreate):
    pass


@dataclass(frozen=True)
class ContactDuplicateWarning:
    contact_id: str
    full_name: str
    reason: str
    detail: str


def find_contact_duplicate_warnings(
    contacts: list[dict[str, Any]],
    *,
    profile_url: str | None,
    email: str | None,
    full_name: str | None,
    company_id: UUID | None,
    exclude_contact_id: UUID | None = None,
) -> list[ContactDuplicateWarning]:
    normalized_profile = normalize_profile_url(profile_url) if profile_url else None
    normalized_email = normalize_email(email) if email else None
    normalized_name = normalize_contact_name(full_name) if full_name else None
    warnings: list[ContactDuplicateWarning] = []

    for contact in contacts:
        if contact.get("archived_at") is not None:
            continue
        if exclude_contact_id is not None and str(contact.get("id")) == str(exclude_contact_id):
            continue
        contact_id = str(contact["id"])
        contact_name = str(contact.get("full_name") or contact_id)

        if normalized_profile:
            try:
                other_profile = normalize_profile_url(contact.get("profile_url"))
            except ValueError:
                other_profile = None
            if other_profile == normalized_profile:
                warnings.append(
                    ContactDuplicateWarning(
                        contact_id=contact_id,
                        full_name=contact_name,
                        reason="profile_url",
                        detail=normalized_profile,
                    )
                )

        if normalized_email:
            other_email = normalize_email(contact.get("email"))
            if other_email == normalized_email:
                warnings.append(
                    ContactDuplicateWarning(
                        contact_id=contact_id,
                        full_name=contact_name,
                        reason="email",
                        detail=normalized_email,
                    )
                )

        if normalized_name and company_id is not None:
            other_name = normalize_contact_name(contact.get("full_name"))
            other_company = contact.get("company_id")
            if other_name == normalized_name and other_company is not None:
                if str(other_company) == str(company_id):
                    warnings.append(
                        ContactDuplicateWarning(
                            contact_id=contact_id,
                            full_name=contact_name,
                            reason="name_company",
                            detail=f"{normalized_name} @ {company_id}",
                        )
                    )

    return warnings


def format_last_interaction(value: date | datetime | None) -> str:
    if value is None:
        return "Never"
    if isinstance(value, datetime):
        return value.date().isoformat()
    return value.isoformat()
