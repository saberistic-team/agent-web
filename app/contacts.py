"""Contact field registries, validation, and duplicate helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

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
    "strong": "Strong",
    "warm": "Warm",
    "neutral": "Neutral",
    "cold": "Cold",
    "unknown": "Unknown",
}
EMAIL_PROVENANCES: dict[str, str] = {
    "manual": "Manual entry",
    "introduction": "Introduction",
    "public_profile": "Public profile",
    "import": "Import",
    "event": "Event",
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
    parsed = urlparse(text if "://" in text else f"https://{text}")
    if not parsed.scheme or not parsed.netloc:
        raise ValueError("profile URL must be a valid URL")
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = parsed.path.rstrip("/") or "/"
    return f"{host}{path}"


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
        role = value.strip()
        if not role or role in seen:
            continue
        if role not in BUYING_ROLES:
            raise ValueError(f"unknown buying role: {role}")
        seen.add(role)
        normalized.append(role)
    return normalized


class ContactCreate(BaseModel):
    full_name: str = Field(min_length=1, max_length=500)
    company_id: UUID
    title: str | None = Field(default=None, max_length=500)
    profile_url: str | None = Field(default=None, max_length=2000)
    email: str | None = Field(default=None, max_length=320)
    email_permitted: bool | None = None
    email_provenance: str | None = None
    last_interaction_at: datetime | None = None
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

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str | None) -> str | None:
        return normalize_email(value)

    @field_validator("profile_url")
    @classmethod
    def validate_profile_url(cls, value: str | None) -> str | None:
        return normalize_profile_url(value)

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
    def validate_roles(cls, value: list[str] | None) -> list[str]:
        return _validate_buying_roles(value)

    @model_validator(mode="after")
    def email_permission_requires_email(self) -> "ContactCreate":
        if self.email is None and (self.email_permitted is not None or self.email_provenance):
            raise ValueError("email provenance requires an email address")
        return self


class ContactUpdate(ContactCreate):
    pass


@dataclass(frozen=True)
class ContactDuplicateWarning:
    contact_id: str
    full_name: str
    reason: str


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
                    full_name=str(contact.get("full_name") or contact["id"]),
                    reason="profile URL",
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
        other = normalize_email(contact.get("email"))
        if other == normalized:
            warnings.append(
                ContactDuplicateWarning(
                    contact_id=str(contact["id"]),
                    full_name=str(contact.get("full_name") or contact["id"]),
                    reason="email",
                )
            )
    return warnings


def find_name_company_duplicate_warnings(
    contacts: list[dict[str, Any]],
    *,
    full_name: str,
    company_id: UUID,
    exclude_contact_id: UUID | None = None,
) -> list[ContactDuplicateWarning]:
    normalized_name = normalize_contact_name(full_name)
    if normalized_name is None:
        return []
    compare_name = normalized_name.casefold()
    warnings: list[ContactDuplicateWarning] = []
    for contact in contacts:
        if contact.get("archived_at") is not None:
            continue
        if exclude_contact_id is not None and str(contact.get("id")) == str(exclude_contact_id):
            continue
        if str(contact.get("company_id")) != str(company_id):
            continue
        other_name = normalize_contact_name(contact.get("full_name"))
        if other_name and other_name.casefold() == compare_name:
            warnings.append(
                ContactDuplicateWarning(
                    contact_id=str(contact["id"]),
                    full_name=str(contact.get("full_name") or contact["id"]),
                    reason="name at company",
                )
            )
    return warnings


def merge_duplicate_warnings(*groups: list[ContactDuplicateWarning]) -> list[ContactDuplicateWarning]:
    seen: set[tuple[str, str]] = set()
    merged: list[ContactDuplicateWarning] = []
    for group in groups:
        for warning in group:
            key = (warning.contact_id, warning.reason)
            if key in seen:
                continue
            seen.add(key)
            merged.append(warning)
    return merged


def format_buying_roles(values: list[str] | None) -> str:
    if not values:
        return "—"
    return ", ".join(BUYING_ROLES.get(value, value) for value in values)
