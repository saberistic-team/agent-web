"""Research record types, provenance validation, and render helpers."""

from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator, model_validator

RESEARCH_RECORD_TYPES: frozenset[str] = frozenset(
    {
        "verified_fact",
        "public_signal",
        "relationship_context",
        "hypothesis",
        "outreach_angle",
        "follow_up_note",
    }
)

PUBLIC_EVIDENCE_TYPES: frozenset[str] = frozenset({"verified_fact", "public_signal"})

RECORD_TYPE_LABELS: dict[str, str] = {
    "verified_fact": "Verified fact",
    "public_signal": "Public signal",
    "relationship_context": "Relationship context",
    "hypothesis": "Hypothesis",
    "outreach_angle": "Outreach angle",
    "follow_up_note": "Follow-up note",
}

# UI distinction groups for fact / signal / hypothesis acceptance criteria.
RECORD_UI_CATEGORIES: dict[str, str] = {
    "verified_fact": "fact",
    "public_signal": "signal",
    "hypothesis": "hypothesis",
    "relationship_context": "context",
    "outreach_angle": "angle",
    "follow_up_note": "note",
}

_UNSAFE_URL_SCHEMES = frozenset(
    {
        "javascript",
        "data",
        "vbscript",
        "file",
    }
)


def record_ui_category(record_type: str) -> str:
    return RECORD_UI_CATEGORIES.get(record_type, "note")


def is_public_evidence_type(record_type: str) -> bool:
    return record_type in PUBLIC_EVIDENCE_TYPES


def validate_source_url(url: str) -> str:
    """Validate a source URL for safe http(s) rendering."""
    stripped = url.strip()
    if not stripped:
        raise ValueError("source URL must not be empty")
    parsed = urlparse(stripped)
    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https"}:
        raise ValueError("source URL must use http or https")
    if scheme in _UNSAFE_URL_SCHEMES:
        raise ValueError("source URL scheme is not allowed")
    if not parsed.netloc:
        raise ValueError("source URL must include a host")
    if re.search(r"[\x00-\x1f\x7f]", stripped):
        raise ValueError("source URL contains invalid characters")
    return stripped


def safe_source_link(url: str, *, label: str | None = None) -> str:
    """Return an escaped anchor for a validated source URL."""
    validated = validate_source_url(url)
    text = html.escape(label or validated)
    href = html.escape(validated, quote=True)
    return (
        f'<a class="research-source-link" href="{href}" '
        f'rel="noopener noreferrer" target="_blank">{text}</a>'
    )


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


def is_stale(record: dict[str, Any], *, now: datetime | None = None) -> bool:
    """Return True when evidence has passed its expiration date."""
    if not is_public_evidence_type(str(record.get("record_type", ""))):
        return False
    expires_at = record.get("expires_at")
    if expires_at is None:
        return False
    if isinstance(expires_at, str):
        expires_at = parse_optional_datetime(expires_at)
    if expires_at is None:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    reference = now or datetime.now(timezone.utc)
    return expires_at <= reference


def format_record_timestamp(value: datetime | str | None) -> str:
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


class ResearchRecordCreate(BaseModel):
    record_type: str = Field(..., min_length=1, max_length=64)
    body: str = Field(..., min_length=1, max_length=10_000)
    contact_id: str | None = Field(default=None, max_length=64)
    source_name: str | None = Field(default=None, max_length=500)
    source_url: str | None = Field(default=None, max_length=2000)
    observed_value: str | None = Field(default=None, max_length=10_000)
    observed_at: str | None = Field(default=None, max_length=64)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    review_at: str | None = Field(default=None, max_length=64)
    expires_at: str | None = Field(default=None, max_length=64)

    @field_validator("record_type")
    @classmethod
    def validate_record_type(cls, value: str) -> str:
        normalized = value.strip()
        if normalized not in RESEARCH_RECORD_TYPES:
            allowed = ", ".join(sorted(RESEARCH_RECORD_TYPES))
            raise ValueError(f"record_type must be one of: {allowed}")
        return normalized

    @field_validator("body", "source_name", "observed_value")
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be empty when provided")
        return stripped

    @field_validator("source_url")
    @classmethod
    def validate_optional_source_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            return None
        return validate_source_url(stripped)

    @model_validator(mode="after")
    def require_public_evidence_fields(self) -> ResearchRecordCreate:
        if not is_public_evidence_type(self.record_type):
            return self
        missing: list[str] = []
        if not self.source_name:
            missing.append("source_name")
        if not self.source_url:
            missing.append("source_url")
        if not self.observed_value:
            missing.append("observed_value")
        if not self.observed_at:
            missing.append("observed_at")
        if self.confidence is None:
            missing.append("confidence")
        if not self.review_at:
            missing.append("review_at")
        if not self.expires_at:
            missing.append("expires_at")
        if missing:
            raise ValueError(
                "public evidence requires: " + ", ".join(missing)
            )
        return self

    def parsed_observed_at(self) -> datetime | None:
        return parse_optional_datetime(self.observed_at)

    def parsed_review_at(self) -> datetime | None:
        return parse_optional_datetime(self.review_at)

    def parsed_expires_at(self) -> datetime | None:
        return parse_optional_datetime(self.expires_at)
