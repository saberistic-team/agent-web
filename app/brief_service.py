"""Read-only admin queries for submitted project briefs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

import psycopg

from app.repositories.postgres import get_repositories
from app.repositories.protocols import ProjectBriefRepository

VALID_STATUSES = frozenset({"pending_payment", "paid", "abandoned"})
MAX_QUERY_LENGTH = 100


@dataclass(frozen=True)
class BriefListFilters:
    page: int
    per_page: int
    query: str | None
    status: str | None
    date_from: date | None
    date_to: date | None
    date_from_raw: str | None
    date_to_raw: str | None


def _parse_date_param(value: str | None) -> date | None:
    if value is None:
        return None
    trimmed = value.strip()[:10]
    if not trimmed:
        return None
    try:
        return date.fromisoformat(trimmed)
    except ValueError:
        return None


def normalize_filters(
    *,
    page: int = 1,
    per_page: int = 50,
    query: str | None = None,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> BriefListFilters:
    """Validate and bound list parameters for the admin briefs page."""
    safe_page = max(page, 1)
    safe_per_page = min(max(per_page, 1), 100)
    safe_query: str | None = None
    if query is not None:
        trimmed = query.strip()
        if trimmed:
            safe_query = trimmed[:MAX_QUERY_LENGTH]
    safe_status: str | None = None
    if status is not None:
        candidate = status.strip()
        if candidate in VALID_STATUSES:
            safe_status = candidate
    parsed_from = _parse_date_param(date_from)
    parsed_to = _parse_date_param(date_to)
    if parsed_from and parsed_to and parsed_from > parsed_to:
        parsed_from, parsed_to = parsed_to, parsed_from
    from_raw = date_from.strip()[:10] if date_from and date_from.strip() else None
    to_raw = date_to.strip()[:10] if date_to and date_to.strip() else None
    return BriefListFilters(
        page=safe_page,
        per_page=safe_per_page,
        query=safe_query,
        status=safe_status,
        date_from=parsed_from,
        date_to=parsed_to,
        date_from_raw=from_raw,
        date_to_raw=to_raw,
    )


def list_briefs(
    conn: psycopg.Connection,
    *,
    page: int = 1,
    per_page: int = 50,
    query: str | None = None,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    repository: ProjectBriefRepository | None = None,
) -> tuple[list[dict[str, Any]], int, BriefListFilters]:
    """Return a paginated, newest-first brief list page."""
    filters = normalize_filters(
        page=page,
        per_page=per_page,
        query=query,
        status=status,
        date_from=date_from,
        date_to=date_to,
    )
    repo = repository or get_repositories().project_briefs
    rows, total = repo.list_page(
        conn,
        page=filters.page,
        per_page=filters.per_page,
        query=filters.query,
        status=filters.status,
        date_from=filters.date_from,
        date_to=filters.date_to,
    )
    return rows, total, filters


def normalize_list_back_params(
    *,
    page: int = 1,
    q: str | None = None,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> BriefListFilters:
    """Validate optional list query params carried on a detail-page back link."""
    return normalize_filters(
        page=page,
        per_page=50,
        query=q,
        status=status,
        date_from=date_from,
        date_to=date_to,
    )


def get_brief(
    conn: psycopg.Connection,
    brief_id: int,
    *,
    repository: ProjectBriefRepository | None = None,
) -> dict[str, Any] | None:
    """Return one project brief by ID, or None when the ID is invalid or missing."""
    if brief_id < 1:
        return None
    repo = repository or get_repositories().project_briefs
    return repo.get_by_id(conn, brief_id)


def preview_brief_detail(brief_id: int) -> dict[str, Any] | None:
    """Synthetic brief rows for ADMIN_PREVIEW_MODE screenshots only."""
    if brief_id == 1:
        return {
            "id": 1,
            "created_at": datetime(2026, 7, 14, 10, 30, tzinfo=timezone.utc),
            "website": "https://acme.example/products/platform",
            "contact_method": "email",
            "contact_value": "ops@acme.example",
            "brief": (
                "We need a technical architecture review for our payments platform. "
                "Scope includes API boundaries, data retention, and rollout sequencing."
            ),
            "status": "paid",
            "stripe_session_id": "cs_preview_session_abc123",
            "stripe_payment_intent_id": "pi_preview_intent_xyz789",
            "paid_at": datetime(2026, 7, 14, 10, 45, tzinfo=timezone.utc),
            "utm_source": "linkedin",
            "utm_medium": "social",
            "utm_campaign": "spring-launch",
            "utm_content": "cta-primary",
            "utm_term": "architecture",
        }
    if brief_id == 2:
        return {
            "id": 2,
            "created_at": datetime(2026, 7, 13, 8, 15, tzinfo=timezone.utc),
            "website": "https://very-long-subdomain-name.example.co.uk/path/to/resource?query=value",
            "contact_method": "email",
            "contact_value": "founder@startup.example",
            "brief": (
                "A" * 400
                + "\n\nSecond paragraph with <script>alert(1)</script> and more detail."
            ),
            "status": "pending_payment",
            "stripe_session_id": None,
            "stripe_payment_intent_id": None,
            "paid_at": None,
            "utm_source": None,
            "utm_medium": None,
            "utm_campaign": None,
            "utm_content": None,
            "utm_term": None,
        }
    return None
