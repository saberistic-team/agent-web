"""Pydantic models for project brief requests."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class BriefCreateRequest(BaseModel):
    website: str = Field(..., min_length=1, max_length=500)
    contact_method: Literal["email", "phone"]
    contact_value: str = Field(..., min_length=1, max_length=320)
    brief: str = Field(..., min_length=1, max_length=10_000)

    @field_validator("website", "contact_value", "brief")
    @classmethod
    def strip_whitespace(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be empty")
        return stripped

    @field_validator("contact_value")
    @classmethod
    def validate_contact_value(cls, value: str, info) -> str:
        method = info.data.get("contact_method")
        if method == "email" and "@" not in value:
            raise ValueError("invalid email address")
        if method == "phone" and len(value) < 7:
            raise ValueError("invalid phone number")
        return value


class BriefCreateResponse(BaseModel):
    checkout_url: str
    brief_id: int
