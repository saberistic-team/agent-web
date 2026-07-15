"""Contact field registries, validation, and duplicate helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
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

# Roles that satisfy acquisition-dashboard "decision-maker" coverage. Influencer does
# not qualify — they may shape deals but are not primary buying decision-makers.
# Investor and introducer are relationship paths, not target outreach owners.
DECISION_MAKER_BUYING_ROLES: frozenset[str] = frozenset(
    role for role in BUYING_ROLES if role in ("founder", "technical_buyer", "executive_buyer")
)
RELATIONSHIP_STRENGTHS: dict[str, str] = {
    "cold": "Cold",
    "developing": "Developing",
    "warm": "Warm",
    "strong": "Strong",
    "champion": "Champion",
}
EMAIL_PERMISSIONS: dict[str, str] = {
    "unknown": "Unknown",
    "inferred": "Inferred from public source",
    "permitted": "Outreach permitted",
    "restricted": "Do not email",
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
    text = value.strip()
    if not text:
        return None
    email = text.lower()
    if "@" not in email or email.startswith("@") or email.endswith("@"):
        raise ValueError("email must be a valid address")
    return email


def normalize_profile_url(value: str | None) -> str | None:
    """Return a comparable profile URL, or None for an intentionally empty field."""
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    parsed = urlparse(text if "://" in text else f"https://{text}", scheme="https")
    if not parsed.hostname:
        raise ValueError("profile URL must be a valid URL")
    host = parsed.hostname.rstrip(".").lower()
    if host.startswith("www."):
        host = host[4:]
    path = parsed.path.rstrip("/") or ""
    return f"https://{host}{path}".lower()


def _validate_registry(value: str | None, registry: dict[str, str], field: str) -> str | None:
    if value is None or not value.strip():
        return None
    if value not in registry:
        raise ValueError(f"unknown {field}: {value}")
    return value


def _validate_buying_roles(values: list[str] | None) -> list[str]:
    if not values:
        return []
    normalized: list[str] = []
    for value in values:
        if not value or not value.strip():
            continue
        if value not in BUYING_ROLES:
            raise ValueError(f"unknown buying role: {value}")
        if value not in normalized:
            normalized.append(value)
    return normalized


class ContactCreate(BaseModel):
    full_name: str = Field(min_length=1, max_length=500)
    title: str | None = Field(default=None, max_length=500)
    profile_url: str | None = Field(default=None, max_length=2000)
    email: str | None = Field(default=None, max_length=320)
    email_permission: str | None = None
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

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str | None) -> str | None:
        return normalize_contact_name(value)

    @field_validator("profile_url")
    @classmethod
    def validate_profile_url(cls, value: str | None) -> str | None:
        return normalize_profile_url(value)

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str | None) -> str | None:
        return normalize_email(value)

    @field_validator("email_permission")
    @classmethod
    def validate_email_permission(cls, value: str | None) -> str | None:
        return _validate_registry(value, EMAIL_PERMISSIONS, "email permission")

    @field_validator("relationship_strength")
    @classmethod
    def validate_relationship_strength(cls, value: str | None) -> str | None:
        return _validate_registry(value, RELATIONSHIP_STRENGTHS, "relationship strength")

    @field_validator("buying_roles")
    @classmethod
    def validate_roles(cls, value: list[str] | None) -> list[str]:
        return _validate_buying_roles(value)


class ContactUpdate(ContactCreate):
    pass


@dataclass(frozen=True)
class ContactDuplicateWarning:
    contact_id: str
    label: str
    match_type: str


def find_profile_url_duplicate_warnings(
    contacts: list[dict[str, Any]],
    *,
    profile_url: str | None,
    exclude_contact_id: UUID | None = None,
) -> list[ContactDuplicateWarning]:
    normalized = normalize_profile_url(profile_url)
    if normalized is None:
        return []
    warnings: list[ContactDuplicateWarning] = []
    for contact in contacts:
        if contact.get("archived_at") is not None:
            continue
        if exclude_contact_id is not None and str(contact.get("id")) == str(exclude_contact_id):
            continue
        try:
            other = normalize_profile_url(contact.get("profile_url"))
        except ValueError:
            continue
        if other == normalized:
            warnings.append(
                ContactDuplicateWarning(
                    contact_id=str(contact["id"]),
                    label=str(contact.get("full_name") or contact.get("email") or contact["id"]),
                    match_type="profile_url",
                )
            )
    return warnings


def find_email_duplicate_warnings(
    contacts: list[dict[str, Any]],
    *,
    email: str | None,
    exclude_contact_id: UUID | None = None,
) -> list[ContactDuplicateWarning]:
    normalized = normalize_email(email)
    if normalized is None:
        return []
    warnings: list[ContactDuplicateWarning] = []
    for contact in contacts:
        if contact.get("archived_at") is not None:
            continue
        if exclude_contact_id is not None and str(contact.get("id")) == str(exclude_contact_id):
            continue
        try:
            other = normalize_email(contact.get("email"))
        except ValueError:
            continue
        if other == normalized:
            warnings.append(
                ContactDuplicateWarning(
                    contact_id=str(contact["id"]),
                    label=str(contact.get("full_name") or other),
                    match_type="email",
                )
            )
    return warnings


def find_name_company_duplicate_warnings(
    contacts: list[dict[str, Any]],
    *,
    full_name: str,
    company_id: UUID | None,
    exclude_contact_id: UUID | None = None,
) -> list[ContactDuplicateWarning]:
    if company_id is None:
        return []
    normalized_name = normalize_contact_name(full_name)
    if normalized_name is None:
        return []
    warnings: list[ContactDuplicateWarning] = []
    for contact in contacts:
        if contact.get("archived_at") is not None:
            continue
        if exclude_contact_id is not None and str(contact.get("id")) == str(exclude_contact_id):
            continue
        if str(contact.get("company_id")) != str(company_id):
            continue
        other_name = normalize_contact_name(contact.get("full_name"))
        if other_name and other_name.lower() == normalized_name.lower():
            warnings.append(
                ContactDuplicateWarning(
                    contact_id=str(contact["id"]),
                    label=other_name,
                    match_type="name_company",
                )
            )
    return warnings


def format_buying_roles(values: list[str] | None) -> str:
    if not values:
        return "—"
    return ", ".join(BUYING_ROLES.get(role, role) for role in values)
