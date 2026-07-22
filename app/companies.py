"""Company field registries, validation, and domain duplicate helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

COMPANY_CATEGORIES: dict[str, str] = {
    "fintech": "Fintech",
    "ai_infrastructure": "AI infrastructure",
    "digital_assets": "Digital assets",
    "investor": "Investor",
    "other": "Other",
}
COMPANY_STAGES: dict[str, str] = {
    "pre_seed": "Pre-seed",
    "seed": "Seed",
    "series_a": "Series A",
    "series_b_plus": "Series B+",
    "public": "Public",
    "bootstrapped": "Bootstrapped",
    "unknown": "Unknown",
}
TARGET_STATUSES: dict[str, str] = {
    "target": "Target",
    "watching": "Watching",
    "not_a_fit": "Not a fit",
    "customer": "Customer",
}
FRESHNESS_FILTERS: dict[str, str] = {
    "fresh": "Verified in the last 30 days",
    "stale": "Not verified in 90+ days",
    "unknown": "Never verified",
}

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_domain(value: str | None) -> str | None:
    """Return a comparable host name, or None for an intentionally empty field."""
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    parsed = urlparse(text if "://" in text else f"//{text}", scheme="https")
    if parsed.username or parsed.password or not parsed.hostname:
        raise ValueError("domain must be a valid hostname")
    host = parsed.hostname.rstrip(".").lower()
    if host.startswith("www."):
        host = host[4:]
    if "." not in host or any(part == "" for part in host.split(".")):
        raise ValueError("domain must be a valid hostname")
    return host


def normalize_company_name(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = _WHITESPACE_RE.sub(" ", value.strip())
    return normalized or None


def _validate_registry(value: str | None, registry: dict[str, str], field: str) -> str | None:
    if value is None or not value.strip():
        return None
    if value not in registry:
        raise ValueError(f"unknown {field}: {value}")
    return value


class CompanyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=500)
    website: str | None = Field(default=None, max_length=2000)
    domain: str | None = Field(default=None, max_length=253)
    category: str | None = None
    stage: str | None = None
    headcount_estimate: int | None = Field(default=None, ge=0)
    funding_summary: str | None = Field(default=None, max_length=2000)
    target_status: str | None = None
    last_verified_at: date | None = None
    notes: str | None = Field(default=None, max_length=10000)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        normalized = normalize_company_name(value)
        if normalized is None:
            raise ValueError("name must not be empty")
        return normalized

    @field_validator("domain")
    @classmethod
    def validate_domain(cls, value: str | None) -> str | None:
        return normalize_domain(value)

    @field_validator("category")
    @classmethod
    def validate_category(cls, value: str | None) -> str | None:
        return _validate_registry(value, COMPANY_CATEGORIES, "category")

    @field_validator("stage")
    @classmethod
    def validate_stage(cls, value: str | None) -> str | None:
        return _validate_registry(value, COMPANY_STAGES, "stage")

    @field_validator("target_status")
    @classmethod
    def validate_target_status(cls, value: str | None) -> str | None:
        return _validate_registry(value, TARGET_STATUSES, "target status")

    @model_validator(mode="after")
    def derive_domain_from_website(self) -> "CompanyCreate":
        if self.domain is None and self.website:
            self.domain = normalize_domain(self.website)
        return self


def company_audit_summary(company: dict[str, Any]) -> dict[str, Any]:
    """Compact company snapshot for audit events.

    Free-form notes, funding text, and website URLs are omitted; presence flags
    and normalized registry fields are stored instead.
    """
    last_verified = company.get("last_verified_at")
    archived_at = company.get("archived_at")
    notes = company.get("notes")
    funding_summary = company.get("funding_summary")
    website = company.get("website")
    return {
        "name": company.get("name"),
        "domain": company.get("domain"),
        "category": company.get("category"),
        "stage": company.get("stage"),
        "headcount_estimate": company.get("headcount_estimate"),
        "target_status": company.get("target_status"),
        "last_verified_at": (
            last_verified.isoformat()
            if hasattr(last_verified, "isoformat")
            else last_verified
        ),
        "archived_at": (
            archived_at.isoformat()
            if hasattr(archived_at, "isoformat")
            else archived_at
        ),
        "has_website": bool(website and str(website).strip()),
        "has_notes": bool(notes and str(notes).strip()),
        "has_funding_summary": bool(
            funding_summary and str(funding_summary).strip()
        ),
    }


class CompanyUpdate(CompanyCreate):
    """Partial company patch.

    Unlike :class:`CompanyCreate`, domain is only derived from the website when
    the ``domain`` field was omitted entirely. When an edit form submits a blank
    ``domain`` it is an explicit clear and must not be silently re-derived.
    """

    @model_validator(mode="after")
    def derive_domain_from_website(self) -> "CompanyUpdate":
        if "domain" not in self.model_fields_set and self.website:
            self.domain = normalize_domain(self.website)
        return self


@dataclass(frozen=True)
class CompanyDuplicateWarning:
    company_id: str
    name: str
    domain: str


def find_domain_duplicate_warnings(
    companies: list[dict[str, Any]],
    *,
    domain: str | None,
    exclude_company_id: UUID | None = None,
) -> list[CompanyDuplicateWarning]:
    normalized = normalize_domain(domain)
    if normalized is None:
        return []
    warnings: list[CompanyDuplicateWarning] = []
    for company in companies:
        if company.get("archived_at") is not None:
            continue
        if exclude_company_id is not None and str(company.get("id")) == str(exclude_company_id):
            continue
        try:
            other = normalize_domain(company.get("domain") or company.get("website"))
        except ValueError:
            continue
        if other == normalized:
            warnings.append(
                CompanyDuplicateWarning(
                    company_id=str(company["id"]),
                    name=str(company.get("name") or company["id"]),
                    domain=normalized,
                )
            )
    return warnings
