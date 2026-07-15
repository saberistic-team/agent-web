"""Brief-to-CRM/pipeline conversion helpers."""

from __future__ import annotations

from typing import Any

from app.companies import normalize_domain
from app.config import Settings
from app.pipeline_stages import initial_pipeline_stage_for_brief_status, pipeline_stage_label


class BriefConversionError(Exception):
    """Base error for brief conversion flows."""


class BriefAlreadyConvertedError(BriefConversionError):
    """Raised when a brief already has a source linkage record."""


class BriefConversionIdempotencyRace(BriefConversionError):
    """Internal signal: source_records uniqueness lost a concurrent conversion race."""


class BriefConversionValidationError(BriefConversionError):
    """Raised when operator input fails validation."""


def normalize_brief_email(value: str) -> str:
    return value.strip().lower()


def derive_company_name(*, website: str, domain: str | None = None) -> str:
    resolved = domain or normalize_domain(website)
    if not resolved:
        return "Unknown company"
    label = resolved.split(".")[0]
    return label.replace("-", " ").replace("_", " ").title()


def pipeline_capabilities_available(settings: Settings) -> bool:
    """True when CRM and pipeline storage can be used for brief conversion."""
    return bool(settings.database_url)


def build_conversion_proposal(
    brief: dict[str, Any],
    *,
    price_cents: int,
) -> dict[str, Any]:
    """Build operator-facing proposed CRM/pipeline fields from a brief row."""
    domain = normalize_domain(str(brief.get("website", "")))
    email = normalize_brief_email(str(brief.get("contact_value", "")))
    brief_status = str(brief.get("status", ""))
    pipeline_stage = initial_pipeline_stage_for_brief_status(brief_status)
    expected_value: float | None = None
    if brief_status == "paid":
        amount_cents = brief.get("payment_amount_cents")
        if amount_cents is None:
            amount_cents = price_cents
        expected_value = round(int(amount_cents) / 100, 2)
    return {
        "company_name": derive_company_name(website=str(brief.get("website", "")), domain=domain),
        "website": str(brief.get("website", "")),
        "domain": domain,
        "contact_email": email,
        "contact_name": None,
        "brief_status": brief_status,
        "pipeline_stage": pipeline_stage,
        "pipeline_stage_label": pipeline_stage_label(pipeline_stage),
        "expected_value": expected_value,
    }


def safe_conversion_payload(brief: dict[str, Any]) -> dict[str, Any]:
    """Redacted-safe metadata for source_records and audit — no brief text or email."""
    return {
        "brief_id": brief.get("id"),
        "brief_status": brief.get("status"),
        "utm_source": brief.get("utm_source"),
        "utm_medium": brief.get("utm_medium"),
        "utm_campaign": brief.get("utm_campaign"),
    }
