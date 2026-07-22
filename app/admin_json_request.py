"""Strict JSON parsing and header CSRF extraction for authenticated admin JSON APIs."""

from __future__ import annotations

import json
from typing import Any

from fastapi import HTTPException, Request

from app import admin_auth
from app.config import Settings

UNSUPPORTED_MEDIA_TYPE_MESSAGE = "Unsupported Media Type"
REQUEST_BODY_TOO_LARGE_MESSAGE = "Request body too large"


def read_session_csrf_header(request: Request) -> str | None:
    """Return the trimmed ``X-CSRF-Token`` header value when present."""
    value = request.headers.get(admin_auth.CSRF_HEADER_NAME)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def verify_session_csrf_header_or_reject(
    request: Request,
    settings: Settings,
    *,
    submitted_csrf_token: str | None,
) -> None:
    """Validate a header-delivered session CSRF token or fail generically."""
    if (
        not submitted_csrf_token
        or len(submitted_csrf_token) > admin_auth.LOGIN_CSRF_MAX_LENGTH
        or not admin_auth.verify_session_csrf_request(request, submitted_csrf_token, settings)
    ):
        raise HTTPException(status_code=400, detail=admin_auth.INVALID_REQUEST_MESSAGE)


def reject_duplicate_csrf_field(payload: dict[str, Any]) -> None:
    """Reject JSON payloads that duplicate CSRF transport in the body."""
    if admin_auth.CSRF_FORM_FIELD in payload:
        raise HTTPException(status_code=400, detail=admin_auth.INVALID_REQUEST_MESSAGE)


def require_json_content_type(request: Request) -> None:
    """Require ``Content-Type: application/json`` before reading the body."""
    content_type = request.headers.get("content-type")
    if not content_type:
        raise HTTPException(status_code=415, detail=UNSUPPORTED_MEDIA_TYPE_MESSAGE)
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        raise HTTPException(status_code=415, detail=UNSUPPORTED_MEDIA_TYPE_MESSAGE)


async def read_bounded_json_object(
    request: Request,
    *,
    max_body_bytes: int,
) -> dict[str, Any]:
    """Parse a bounded JSON object after the caller validates auth and CSRF."""
    require_json_content_type(request)
    body = await request.body()
    if len(body) > max_body_bytes:
        raise HTTPException(status_code=413, detail=REQUEST_BODY_TOO_LARGE_MESSAGE)
    if not body:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=400, detail="JSON object required")
    return parsed
