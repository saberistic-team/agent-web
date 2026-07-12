"""Pydantic models for project brief requests."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class BriefCreateRequest(BaseModel):
    website: str = Field(..., min_length=1, max_length=500)
    email: str = Field(..., min_length=1, max_length=320)
    brief: str = Field(..., min_length=1, max_length=10_000)

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


class BriefCreateResponse(BaseModel):
    checkout_url: str
    brief_id: int
