"""Contact domain types, buying roles, normalization, and duplicate detection."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlparse, urlunparse
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

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

CONTACT_STATUSES: frozenset[str] = frozenset({"active", "archived"})

RELATIONSHIP_STRENGTH_MIN = 1
RELATIONSHIP_STRENGTH_MAX = 5

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_email(email: str | None) -> str | None:
    if email is None:
        return None
    stripped = email.strip()
    if not stripped:
        return None
    return stripped.lower()


def normalize_name(name: str | None) -> str | None:
    if name is None:
        return None
    collapsed = _WHITESPACE_RE.sub(" ", name.strip())
    if not collapsed:
        return None
    return collapsed.casefold()


def normalize_profile_url(url: str | None) -> str | None:
    """Normalize profile URLs for duplicate comparison."""
    if url is None:
        return None
    stripped = url.strip()
    if not stripped:
        return None
    parsed = urlparse(stripped if "://" in stripped else f"https://{stripped}")
    scheme = (parsed.scheme or "https").lower()
    if scheme not in {"http", "https"}:
        return stripped.rstrip("/").lower()
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = parsed.path.rstrip("/") or ""
    # LinkedIn: drop locale prefix (/en, /in) for person/company paths.
    if host in {"linkedin.com", "www.linkedin.com"}:
        parts = [part for part in path.split("/") if part]
        if len(parts) >= 2 and parts[0] in {"in", "pub"}:
            path = "/" + "/".join(parts)
        elif len(parts) >= 2:
            path = "/" + "/".join(parts[:2])
    netloc = host
    if parsed.port and parsed.port not in {80, 443}:
        netloc = f"{host}:{parsed.port}"
    normalized = urlunparse((scheme, netloc, path, "", "", ""))
    return normalized.rstrip("/").lower()


class ContactCreate(BaseModel):
    full_name: str = Field(min_length=1, max_length=500)
    company_id: UUID | None = None
    email: str | None = Field(default=None, max_length=500)
    title: str | None = Field(default=None, max_length=500)
    profile_url: str | None = Field(default=None, max_length=2000)
    email_provenance: str | None = Field(default=None, max_length=500)
    email_permission: str | None = Field(default=None, max_length=500)
    last_interaction_at: datetime | None = None
    relationship_strength: int | None = None
    notes: str | None = Field(default=None, max_length=10000)
    buying_roles: list[str] = Field(default_factory=list)

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str | None) -> str | None:
        normalized = normalize_email(value)
        if normalized and "@" not in normalized:
            raise ValueError("email must contain @")
        return normalized

    @field_validator("buying_roles")
    @classmethod
    def validate_buying_roles(cls, roles: list[str]) -> list[str]:
        unique: list[str] = []
        seen: set[str] = set()
        for role in roles:
            if role not in BUYING_ROLES:
                raise ValueError(f"invalid buying role: {role}")
            if role not in seen:
                seen.add(role)
                unique.append(role)
        return unique

    @field_validator("relationship_strength")
    @classmethod
    def validate_relationship_strength(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if value < RELATIONSHIP_STRENGTH_MIN or value > RELATIONSHIP_STRENGTH_MAX:
            raise ValueError(
                f"relationship strength must be between {RELATIONSHIP_STRENGTH_MIN} "
                f"and {RELATIONSHIP_STRENGTH_MAX}"
            )
        return value


class ContactUpdate(BaseModel):
    full_name: str | None = Field(default=None, min_length=1, max_length=500)
    company_id: UUID | None = None
    clear_company: bool = False
    email: str | None = Field(default=None, max_length=500)
    clear_email: bool = False
    title: str | None = Field(default=None, max_length=500)
    clear_title: bool = False
    profile_url: str | None = Field(default=None, max_length=2000)
    clear_profile_url: bool = False
    email_provenance: str | None = Field(default=None, max_length=500)
    clear_email_provenance: bool = False
    email_permission: str | None = Field(default=None, max_length=500)
    clear_email_permission: bool = False
    last_interaction_at: datetime | None = None
    clear_last_interaction: bool = False
    relationship_strength: int | None = None
    clear_relationship_strength: bool = False
    notes: str | None = Field(default=None, max_length=10000)
    clear_notes: bool = False
    buying_roles: list[str] | None = None

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str | None) -> str | None:
        normalized = normalize_email(value)
        if normalized and "@" not in normalized:
            raise ValueError("email must contain @")
        return normalized

    @field_validator("buying_roles")
    @classmethod
    def validate_buying_roles(cls, roles: list[str] | None) -> list[str] | None:
        if roles is None:
            return None
        unique: list[str] = []
        seen: set[str] = set()
        for role in roles:
            if role not in BUYING_ROLES:
                raise ValueError(f"invalid buying role: {role}")
            if role not in seen:
                seen.add(role)
                unique.append(role)
        return unique

    @field_validator("relationship_strength")
    @classmethod
    def validate_relationship_strength(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if value < RELATIONSHIP_STRENGTH_MIN or value > RELATIONSHIP_STRENGTH_MAX:
            raise ValueError(
                f"relationship strength must be between {RELATIONSHIP_STRENGTH_MIN} "
                f"and {RELATIONSHIP_STRENGTH_MAX}"
            )
        return value


@dataclass
class DuplicateWarning:
    reason: str
    contact_id: str
    label: str


def find_duplicate_warnings(
    contacts: list[dict[str, Any]],
    *,
    profile_url: str | None,
    email: str | None,
    full_name: str | None,
    company_id: UUID | None,
    exclude_contact_id: UUID | None = None,
) -> list[DuplicateWarning]:
    """Return non-blocking duplicate warnings for normalized identifiers."""
    norm_profile = normalize_profile_url(profile_url)
    norm_email = normalize_email(email)
    norm_name = normalize_name(full_name)
    company_key = str(company_id) if company_id is not None else None
    warnings: list[DuplicateWarning] = []
    seen: set[tuple[str, str]] = set()

    for contact in contacts:
        contact_id = str(contact["id"])
        if exclude_contact_id is not None and contact_id == str(exclude_contact_id):
            continue
        if str(contact.get("status", "active")) == "archived":
            continue
        label = _contact_label(contact)

        if norm_profile:
            other_profile = normalize_profile_url(contact.get("profile_url"))
            if other_profile and other_profile == norm_profile:
                key = ("profile_url", contact_id)
                if key not in seen:
                    seen.add(key)
                    warnings.append(
                        DuplicateWarning(
                            reason="profile_url",
                            contact_id=contact_id,
                            label=label,
                        )
                    )

        if norm_email:
            other_email = normalize_email(contact.get("email"))
            if other_email and other_email == norm_email:
                key = ("email", contact_id)
                if key not in seen:
                    seen.add(key)
                    warnings.append(
                        DuplicateWarning(
                            reason="email",
                            contact_id=contact_id,
                            label=label,
                        )
                    )

        if norm_name and company_key:
            other_name = normalize_name(contact.get("full_name"))
            other_company = (
                str(contact["company_id"])
                if contact.get("company_id") is not None
                else None
            )
            if other_name and other_company == company_key and other_name == norm_name:
                key = ("name_company", contact_id)
                if key not in seen:
                    seen.add(key)
                    warnings.append(
                        DuplicateWarning(
                            reason="name_company",
                            contact_id=contact_id,
                            label=label,
                        )
                    )

    return warnings


def _contact_label(contact: dict[str, Any]) -> str:
    name = str(contact.get("full_name") or "").strip()
    email = str(contact.get("email") or "").strip()
    if name and email:
        return f"{name} ({email})"
    return name or email or str(contact.get("id", ""))


def format_buying_roles(roles: list[str]) -> str:
    return ", ".join(BUYING_ROLE_LABELS.get(role, role) for role in roles)


def parse_last_interaction(value: str | None) -> datetime | None:
    if value is None or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)
