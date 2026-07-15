"""Contact field registries, validation, and domain duplicate helpers."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime
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
    "cold": "Cold",
    "warm": "Warm",
    "strong": "Strong",
    "champion": "Champion",
}
EMAIL_PERMISSIONS: dict[str, str] = {
    "permitted": "Permitted to contact",
    "referral_only": "Referral only",
    "public_source": "Public source",
    "unknown": "Unknown",
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
    """Return a comparable profile URL host + path, or None when empty."""
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    parsed = urlparse(text if "://" in text else f"https://{text}")
    if parsed.username or parsed.password or not parsed.hostname:
        raise ValueError("profile URL must be a valid URL")
    host = parsed.hostname.rstrip(".").lower()
    if host.startswith("www."):
        host = host[4:]
    path = (parsed.path or "").rstrip("/").lower()
    if not path:
        raise ValueError("profile URL must include a path")
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
    seen: list[str] = []
    for value in values:
        if value not in BUYING_ROLES:
            raise ValueError(f"unknown buying role: {value}")
        if value not in seen:
            seen.append(value)
    return seen


def parse_buying_roles(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        if not raw.strip():
            return []
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return _validate_buying_roles([raw])
        if isinstance(parsed, list):
            return _validate_buying_roles([str(item) for item in parsed])
        return []
    if isinstance(raw, (list, tuple)):
        return _validate_buying_roles([str(item) for item in raw])
    return []


def format_buying_roles(values: list[str] | Any) -> str:
    roles = parse_buying_roles(values)
    if not roles:
        return "—"
    return ", ".join(BUYING_ROLES.get(role, role) for role in roles)


class ContactCreate(BaseModel):
    full_name: str = Field(min_length=1, max_length=500)
    company_id: UUID | None = None
    title: str | None = Field(default=None, max_length=500)
    profile_url: str | None = Field(default=None, max_length=2000)
    email: str | None = Field(default=None, max_length=320)
    email_permission: str | None = None
    email_source: str | None = Field(default=None, max_length=500)
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

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str | None) -> str | None:
        return normalize_email(value)

    @field_validator("profile_url")
    @classmethod
    def validate_profile_url(cls, value: str | None) -> str | None:
        return normalize_profile_url(value)

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
    def validate_buying_roles(cls, value: list[str]) -> list[str]:
        return _validate_buying_roles(value)

    @model_validator(mode="after")
    def email_permission_requires_email(self) -> "ContactCreate":
        if self.email_permission is not None and self.email is None:
            raise ValueError("email permission requires an email address")
        return self


class ContactUpdate(ContactCreate):
    pass


@dataclass(frozen=True)
class ContactDuplicateWarning:
    contact_id: str
    full_name: str
    reason: str


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
    normalized_email = normalize_email(email)
    normalized_name = normalize_contact_name(full_name)
    warnings: list[ContactDuplicateWarning] = []
    seen: set[tuple[str, str]] = set()

    for contact in contacts:
        if contact.get("archived_at") is not None:
            continue
        if exclude_contact_id is not None and str(contact.get("id")) == str(exclude_contact_id):
            continue
        contact_id = str(contact["id"])
        contact_name = str(contact.get("full_name") or contact_id)

        other_profile = contact.get("profile_url_normalized") or contact.get("profile_url")
        try:
            other_profile_normalized = normalize_profile_url(other_profile)
        except ValueError:
            other_profile_normalized = None
        if normalized_profile and other_profile_normalized == normalized_profile:
            key = ("profile", contact_id)
            if key not in seen:
                seen.add(key)
                warnings.append(
                    ContactDuplicateWarning(
                        contact_id=contact_id,
                        full_name=contact_name,
                        reason="profile URL",
                    )
                )

        other_email = normalize_email(contact.get("email"))
        if normalized_email and other_email == normalized_email:
            key = ("email", contact_id)
            if key not in seen:
                seen.add(key)
                warnings.append(
                    ContactDuplicateWarning(
                        contact_id=contact_id,
                        full_name=contact_name,
                        reason="email",
                    )
                )

        other_name = normalize_contact_name(contact.get("full_name"))
        other_company = contact.get("company_id")
        if (
            normalized_name
            and company_id is not None
            and other_name == normalized_name
            and other_company is not None
            and str(other_company) == str(company_id)
        ):
            key = ("name_company", contact_id)
            if key not in seen:
                seen.add(key)
                warnings.append(
                    ContactDuplicateWarning(
                        contact_id=contact_id,
                        full_name=contact_name,
                        reason="name and company",
                    )
                )
    return warnings
