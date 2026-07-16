"""Pydantic models for project brief requests."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from app.analytics_event_schema import is_valid_anonymous_session_id


class BriefCreateRequest(BaseModel):
    website: str = Field(..., min_length=1, max_length=500)
    email: str = Field(..., min_length=1, max_length=320)
    brief: str = Field(..., min_length=1, max_length=10_000)
    utm_source: str | None = Field(default=None, max_length=200)
    utm_medium: str | None = Field(default=None, max_length=200)
    utm_campaign: str | None = Field(default=None, max_length=200)
    utm_content: str | None = Field(default=None, max_length=200)
    utm_term: str | None = Field(default=None, max_length=200)
    analytics_session_id: str | None = Field(default=None, max_length=36)

    @field_validator("website", "email", "brief")
    @classmethod
    def strip_whitespace(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be empty")
        return stripped

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        if "@" not in value:
            raise ValueError("invalid email address")
        return value

    @field_validator("analytics_session_id")
    @classmethod
    def validate_analytics_session_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            return None
        if not is_valid_anonymous_session_id(stripped):
            raise ValueError("analytics_session_id must be an opaque UUID")
        return stripped

    def approved_analytics_session_id(self) -> str | None:
        """Return session token only when explicitly supplied and valid."""
        return self.analytics_session_id

    def utm_attribution(self) -> dict[str, str | None]:
        return {
            "utm_source": self.utm_source,
            "utm_medium": self.utm_medium,
            "utm_campaign": self.utm_campaign,
            "utm_content": self.utm_content,
            "utm_term": self.utm_term,
        }


class BriefCreateResponse(BaseModel):
    checkout_url: str
    brief_id: int
