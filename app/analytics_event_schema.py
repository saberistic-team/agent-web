"""Versioned first-party analytics event contract (schema v1).

Models the privacy-conscious event payload before transport or storage.
Preserves funnel terminology from docs/ANALYTICS_FUNNEL.md while separating
engagement events from conversion steps.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

# Current schema version. Bump major for breaking contract changes.
SCHEMA_VERSION: Literal["1.0.0"] = "1.0.0"

# Anonymous session identity retention and rotation (documented in ANALYTICS_EVENT_SCHEMA.md).
ANONYMOUS_SESSION_MAX_AGE_SECONDS = 86_400  # 24 hours wall-clock
ANONYMOUS_SESSION_ROTATION_SECONDS = 1_800  # 30 minutes inactivity

_ANON_SESSION_ID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

# Engagement (page / nav) — no authoritative funnel truth.
EVENT_LANDING_VIEWED = "Landing Viewed"
EVENT_ABOUT_VIEWED = "About Viewed"
EVENT_SERVICES_VIEWED = "Services Viewed"
EVENT_CASE_STUDIES_VIEWED = "Case Studies Viewed"
EVENT_CASE_STUDY_VIEWED = "Case Study Viewed"
EVENT_INSIGHTS_VIEWED = "Insights Viewed"
EVENT_INSIGHT_VIEWED = "Insight Viewed"

EVENT_NAV_SERVICES = "Nav Services"
EVENT_NAV_CASE_STUDIES = "Nav Case Studies"
EVENT_NAV_INSIGHTS = "Nav Insights"
EVENT_NAV_DIAGNOSTIC = "Nav Diagnostic"

# Conversion funnel — server events at steps 5–7 are authoritative.
EVENT_BRIEF_VIEWED = "Brief Viewed"
EVENT_BRIEF_FORM_STARTED = "Brief Form Started"
EVENT_LEAD_PERSISTED = "Lead Persisted"
EVENT_CHECKOUT_OPENED = "Checkout Opened"
EVENT_PAYMENT_COMPLETED = "Payment Completed"
EVENT_CONTACT_INITIATED = "Contact Initiated"
EVENT_CHECKOUT_CANCELLED = "Checkout Cancelled"
EVENT_BRIEF_SUCCESS_VIEWED = "Brief Success Viewed"

ENGAGEMENT_EVENT_NAMES = frozenset(
    {
        EVENT_LANDING_VIEWED,
        EVENT_ABOUT_VIEWED,
        EVENT_SERVICES_VIEWED,
        EVENT_CASE_STUDIES_VIEWED,
        EVENT_CASE_STUDY_VIEWED,
        EVENT_INSIGHTS_VIEWED,
        EVENT_INSIGHT_VIEWED,
        EVENT_NAV_SERVICES,
        EVENT_NAV_CASE_STUDIES,
        EVENT_NAV_INSIGHTS,
        EVENT_NAV_DIAGNOSTIC,
    }
)

CONVERSION_EVENT_NAMES = frozenset(
    {
        EVENT_BRIEF_VIEWED,
        EVENT_BRIEF_FORM_STARTED,
        EVENT_LEAD_PERSISTED,
        EVENT_CHECKOUT_OPENED,
        EVENT_PAYMENT_COMPLETED,
        EVENT_CONTACT_INITIATED,
        EVENT_CHECKOUT_CANCELLED,
        EVENT_BRIEF_SUCCESS_VIEWED,
    }
)

ALLOWED_EVENT_NAMES = ENGAGEMENT_EVENT_NAMES | CONVERSION_EVENT_NAMES

# Properties that must never appear in analytics payloads.
SENSITIVE_PROPERTY_NAMES = frozenset(
    {
        "email",
        "phone",
        "website",
        "brief",
        "contact_value",
        "contact_method",
        "wallet_address",
        "stripe_session_id",
        "stripe_payment_intent_id",
        "checkout_url",
        "session_id",
        "payment_intent",
        "url",
        "submitted_url",
        "query_string",
        "raw_query",
        "referrer_url",
        "external_url",
        "ip_address",
        "client_ip",
        "remote_addr",
        "fingerprint",
        "device_fingerprint",
        "canvas_hash",
        "user_agent",
        "ua_hash",
        "user_agent_hash",
    }
)

_SENSITIVE_KEY_PATTERN = re.compile(
    r"(email|phone|wallet|fingerprint|user[_-]?agent|ip[_-]?addr|payment[_-]?intent|"
    r"stripe[_-]?session|checkout[_-]?url|query[_-]?string)",
    re.IGNORECASE,
)

_EMAIL_PATTERN = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_URL_PATTERN = re.compile(r"https?://", re.IGNORECASE)

# Allowlisted custom event properties (lowercase keys after normalization).
ALLOWED_PROPERTY_NAMES = frozenset(
    {
        "brief_id",
        "price_cents",
        "environment",
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_content",
        "utm_term",
        "page",
        "contact_channel",
        "funnel_step",
        "case_study_slug",
        "article_slug",
        "nav_destination",
        "linkage_source",
    }
)

UTM_ATTRIBUTION_KEYS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_content",
        "utm_term",
    }
)

VALID_FUNNEL_STEPS = frozenset({1, 3, 4, 5, 6, 7, 8})

# Event-specific property constraints (beyond global allowlist).
_EVENT_REQUIRED_PROPERTIES: dict[str, frozenset[str]] = {
    EVENT_LEAD_PERSISTED: frozenset({"brief_id", "funnel_step"}),
    EVENT_CHECKOUT_OPENED: frozenset({"brief_id", "price_cents", "funnel_step"}),
    EVENT_PAYMENT_COMPLETED: frozenset({"brief_id", "price_cents", "funnel_step"}),
    EVENT_CASE_STUDY_VIEWED: frozenset({"case_study_slug"}),
    EVENT_INSIGHT_VIEWED: frozenset({"article_slug"}),
    EVENT_CONTACT_INITIATED: frozenset({"contact_channel", "funnel_step"}),
}

_EVENT_FORBIDDEN_PROPERTIES: dict[str, frozenset[str]] = {
    EVENT_LANDING_VIEWED: frozenset({"brief_id", "price_cents"}),
    EVENT_ABOUT_VIEWED: frozenset({"brief_id", "price_cents"}),
    EVENT_SERVICES_VIEWED: frozenset({"brief_id", "price_cents"}),
    EVENT_CASE_STUDIES_VIEWED: frozenset({"brief_id", "price_cents"}),
    EVENT_INSIGHTS_VIEWED: frozenset({"brief_id", "price_cents"}),
    EVENT_NAV_SERVICES: frozenset({"brief_id", "price_cents", "funnel_step"}),
    EVENT_NAV_CASE_STUDIES: frozenset({"brief_id", "price_cents", "funnel_step"}),
    EVENT_NAV_INSIGHTS: frozenset({"brief_id", "price_cents", "funnel_step"}),
    EVENT_NAV_DIAGNOSTIC: frozenset({"brief_id", "price_cents", "funnel_step"}),
}


class PathClass(str, Enum):
    """Coarse route classification — never a raw or external URL."""

    LANDING = "landing"
    ABOUT = "about"
    SERVICES = "services"
    CASE_STUDIES = "case_studies"
    CASE_STUDY = "case_study"
    INSIGHTS = "insights"
    INSIGHT = "insight"
    BRIEF = "brief"
    BRIEF_SUCCESS = "brief_success"
    UNKNOWN = "unknown"


class ReferrerClass(str, Enum):
    """Coarse referrer bucket — never a full external URL."""

    DIRECT = "direct"
    INTERNAL = "internal"
    SEARCH = "search"
    SOCIAL = "social"
    EMAIL = "email"
    PAID = "paid"
    UNKNOWN_EXTERNAL = "unknown_external"


class ConsentState(str, Enum):
    """Analytics consent posture for the anonymous session."""

    IMPLICIT_ANALYTICS = "implicit_analytics"
    GRANTED = "granted"
    DECLINED = "declined"


class LinkageState(str, Enum):
    """CRM linkage — identified only after explicit brief form submission."""

    ANONYMOUS = "anonymous"
    CRM_BRIEF_LINKED = "crm_brief_linked"


class AnalyticsEventValidationError(ValueError):
    """Raised when a payload violates the analytics event contract."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def classify_path(pathname: str | None) -> PathClass:
    """Map an internal pathname to a privacy-safe path class."""
    if not pathname:
        return PathClass.UNKNOWN
    path = pathname.rstrip("/") or "/"
    if path == "/":
        return PathClass.LANDING
    if path == "/about":
        return PathClass.ABOUT
    if path == "/services":
        return PathClass.SERVICES
    if path == "/case-studies":
        return PathClass.CASE_STUDIES
    if path.startswith("/work/") and path != "/work":
        return PathClass.CASE_STUDY
    if path == "/insights":
        return PathClass.INSIGHTS
    if path.startswith("/insights/") and path != "/insights":
        return PathClass.INSIGHT
    if path == "/brief":
        return PathClass.BRIEF
    if path == "/brief/success":
        return PathClass.BRIEF_SUCCESS
    return PathClass.UNKNOWN


_SEARCH_HOSTS = frozenset(
    {
        "google.",
        "bing.",
        "duckduckgo.",
        "yahoo.",
        "baidu.",
        "yandex.",
    }
)
_SOCIAL_HOSTS = frozenset(
    {
        "linkedin.",
        "twitter.",
        "x.com",
        "facebook.",
        "instagram.",
        "threads.net",
        "mastodon.",
    }
)
_EMAIL_HOSTS = frozenset({"mail.", "outlook.", "gmail."})
_PAID_HOSTS = frozenset({"googleads.", "doubleclick.", "facebook.com/l.php"})


def classify_referrer(
    referrer_host: str | None,
    *,
    site_host: str | None = None,
) -> ReferrerClass:
    """Classify a referrer host into a bucket without storing the full URL."""
    if not referrer_host:
        return ReferrerClass.DIRECT
    host = referrer_host.lower().strip()
    if site_host and host == site_host.lower().strip():
        return ReferrerClass.INTERNAL
    if any(marker in host for marker in _EMAIL_HOSTS):
        return ReferrerClass.EMAIL
    if any(marker in host for marker in _SEARCH_HOSTS):
        return ReferrerClass.SEARCH
    if any(marker in host for marker in _SOCIAL_HOSTS):
        return ReferrerClass.SOCIAL
    if any(marker in host for marker in _PAID_HOSTS):
        return ReferrerClass.PAID
    return ReferrerClass.UNKNOWN_EXTERNAL


def is_valid_anonymous_session_id(value: str) -> bool:
    """Anonymous IDs are opaque UUIDs — never derived from IP or user-agent."""
    if not value or not _ANON_SESSION_ID_PATTERN.match(value):
        return False
    try:
        UUID(value)
    except ValueError:
        return False
    return True


def _is_sensitive_property_key(key: str) -> bool:
    key_lower = key.lower()
    if key_lower in SENSITIVE_PROPERTY_NAMES:
        return True
    return bool(_SENSITIVE_KEY_PATTERN.search(key_lower))


def _is_sensitive_property_value(value: Any) -> bool:
    if value is None or value == "":
        return False
    if isinstance(value, str):
        if _EMAIL_PATTERN.search(value):
            return True
        if _URL_PATTERN.search(value):
            return True
    return False


def filter_properties(props: dict[str, Any] | None) -> dict[str, str | int | bool]:
    """Lenient property filter for transport — drops disallowed keys without raising."""
    if not props:
        return {}
    sanitized: dict[str, str | int | bool] = {}
    for key, value in props.items():
        key_lower = key.lower()
        if _is_sensitive_property_key(key_lower):
            continue
        if key_lower not in ALLOWED_PROPERTY_NAMES:
            continue
        if _is_sensitive_property_value(value):
            continue
        if value is None or value == "":
            continue
        if isinstance(value, bool):
            sanitized[key_lower] = value
        elif isinstance(value, int):
            sanitized[key_lower] = value
        elif isinstance(value, str):
            sanitized[key_lower] = value
        else:
            sanitized[key_lower] = str(value)
    return sanitized


def sanitize_properties(
    props: dict[str, Any] | None,
    *,
    event_name: str | None = None,
) -> dict[str, str | int | bool]:
    """Return only allowlisted, non-sensitive properties; raises on violation."""
    if not props:
        return {}
    if event_name and event_name not in ALLOWED_EVENT_NAMES:
        raise AnalyticsEventValidationError(f"Unknown event name: {event_name}")

    sanitized: dict[str, str | int | bool] = {}
    for key, value in props.items():
        key_lower = key.lower()
        if _is_sensitive_property_key(key_lower):
            raise AnalyticsEventValidationError(
                f"Sensitive property not allowed: {key_lower}"
            )
        if key_lower not in ALLOWED_PROPERTY_NAMES:
            raise AnalyticsEventValidationError(
                f"Disallowed property not in allowlist: {key_lower}"
            )
        if _is_sensitive_property_value(value):
            raise AnalyticsEventValidationError(
                f"Sensitive value pattern in property: {key_lower}"
            )
        if value is None or value == "":
            continue
        if isinstance(value, bool):
            sanitized[key_lower] = value
        elif isinstance(value, int):
            sanitized[key_lower] = value
        elif isinstance(value, str):
            sanitized[key_lower] = value
        else:
            sanitized[key_lower] = str(value)

    if event_name:
        _validate_event_property_constraints(event_name, sanitized)
    return sanitized


def sanitize_attribution(utm: dict[str, str | None] | None) -> dict[str, str]:
    """Return allowlisted UTM attribution fields only."""
    if not utm:
        return {}
    result: dict[str, str] = {}
    for key in UTM_ATTRIBUTION_KEYS:
        value = utm.get(key)
        if not value:
            continue
        if _is_sensitive_property_value(value):
            raise AnalyticsEventValidationError(
                f"Sensitive value pattern in attribution: {key}"
            )
        result[key] = value
    return result


def _validate_event_property_constraints(
    event_name: str,
    props: dict[str, str | int | bool],
) -> None:
    required = _EVENT_REQUIRED_PROPERTIES.get(event_name, frozenset())
    missing = required - frozenset(props)
    if missing:
        raise AnalyticsEventValidationError(
            f"Event {event_name!r} missing required properties: {sorted(missing)}"
        )

    forbidden = _EVENT_FORBIDDEN_PROPERTIES.get(event_name, frozenset())
    present = forbidden & frozenset(props)
    if present:
        raise AnalyticsEventValidationError(
            f"Event {event_name!r} forbids properties: {sorted(present)}"
        )

    funnel_step = props.get("funnel_step")
    if funnel_step is not None and int(funnel_step) not in VALID_FUNNEL_STEPS:
        raise AnalyticsEventValidationError(
            f"Invalid funnel_step: {funnel_step}"
        )


def infer_linkage_state(
    *,
    event_name: str,
    properties: dict[str, str | int | bool],
    explicit: LinkageState | None = None,
) -> LinkageState:
    """Derive linkage state; CRM link requires explicit brief form submission."""
    if explicit is not None:
        return explicit
    if event_name in {EVENT_LEAD_PERSISTED, EVENT_CHECKOUT_OPENED, EVENT_PAYMENT_COMPLETED}:
        return LinkageState.CRM_BRIEF_LINKED
    if properties.get("brief_id") is not None and event_name in {
        EVENT_BRIEF_FORM_STARTED,
        EVENT_CHECKOUT_CANCELLED,
        EVENT_BRIEF_SUCCESS_VIEWED,
    }:
        return LinkageState.CRM_BRIEF_LINKED
    return LinkageState.ANONYMOUS


class AnalyticsEventPayload(BaseModel):
    """Versioned first-party analytics event contract."""

    event_name: str
    schema_version: Literal["1.0.0"] = SCHEMA_VERSION
    occurred_at: datetime
    received_at: datetime | None = None
    anonymous_session_id: str
    path_class: PathClass
    referrer_class: ReferrerClass = ReferrerClass.DIRECT
    attribution: dict[str, str] = Field(default_factory=dict)
    properties: dict[str, str | int | bool] = Field(default_factory=dict)
    consent_state: ConsentState = ConsentState.IMPLICIT_ANALYTICS
    linkage_state: LinkageState = LinkageState.ANONYMOUS

    @field_validator("event_name")
    @classmethod
    def validate_event_name(cls, value: str) -> str:
        if value not in ALLOWED_EVENT_NAMES:
            raise ValueError(f"Unknown event name: {value}")
        return value

    @field_validator("anonymous_session_id")
    @classmethod
    def validate_anonymous_session_id(cls, value: str) -> str:
        if not is_valid_anonymous_session_id(value):
            raise ValueError("anonymous_session_id must be an opaque UUID")
        return value

    @field_validator("attribution")
    @classmethod
    def validate_attribution(cls, value: dict[str, str]) -> dict[str, str]:
        extra = set(value) - UTM_ATTRIBUTION_KEYS
        if extra:
            raise ValueError(f"Attribution keys not allowlisted: {sorted(extra)}")
        for key, attr_value in value.items():
            if _is_sensitive_property_value(attr_value):
                raise ValueError(f"Sensitive value in attribution: {key}")
        return value

    @field_validator("occurred_at", "received_at")
    @classmethod
    def validate_timestamps_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Timestamps must be timezone-aware (UTC)")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def validate_contract(self) -> AnalyticsEventPayload:
        self.properties = sanitize_properties(
            self.properties,
            event_name=self.event_name,
        )
        self.attribution = sanitize_attribution(self.attribution)
        self.linkage_state = infer_linkage_state(
            event_name=self.event_name,
            properties=self.properties,
            explicit=self.linkage_state
            if self.linkage_state != LinkageState.ANONYMOUS
            else None,
        )
        if self.linkage_state == LinkageState.CRM_BRIEF_LINKED:
            if self.properties.get("brief_id") is None:
                raise ValueError(
                    "crm_brief_linked requires brief_id from explicit form submission"
                )
            if self.properties.get("linkage_source") not in (
                None,
                "brief_form_submit",
                "server_brief_persist",
                "server_checkout_open",
                "server_payment_complete",
            ):
                raise ValueError("Invalid linkage_source for crm_brief_linked")
        return self


def build_event_payload(
    *,
    event_name: str,
    anonymous_session_id: str,
    path_class: PathClass | str | None = None,
    pathname: str | None = None,
    referrer_class: ReferrerClass = ReferrerClass.DIRECT,
    occurred_at: datetime | None = None,
    received_at: datetime | None = None,
    attribution: dict[str, str | None] | None = None,
    properties: dict[str, Any] | None = None,
    consent_state: ConsentState = ConsentState.IMPLICIT_ANALYTICS,
    linkage_state: LinkageState | None = None,
) -> AnalyticsEventPayload:
    """Construct and validate a first-party analytics event."""
    if event_name not in ALLOWED_EVENT_NAMES:
        raise AnalyticsEventValidationError(f"Unknown event name: {event_name}")

    resolved_path = (
        path_class
        if isinstance(path_class, PathClass)
        else classify_path(pathname)
        if path_class is None
        else PathClass(path_class)
    )
    safe_props = sanitize_properties(properties, event_name=event_name)
    safe_attribution = sanitize_attribution(attribution)
    resolved_linkage = infer_linkage_state(
        event_name=event_name,
        properties=safe_props,
        explicit=linkage_state,
    )
    try:
        return AnalyticsEventPayload(
            event_name=event_name,
            occurred_at=occurred_at or _utc_now(),
            received_at=received_at,
            anonymous_session_id=anonymous_session_id,
            path_class=resolved_path,
            referrer_class=referrer_class,
            attribution=safe_attribution,
            properties=safe_props,
            consent_state=consent_state,
            linkage_state=resolved_linkage,
        )
    except ValidationError as exc:
        raise AnalyticsEventValidationError(str(exc)) from exc


def parse_event_payload(data: dict[str, Any]) -> AnalyticsEventPayload:
    """Parse and validate a raw dict into a contract-compliant event."""
    try:
        return AnalyticsEventPayload.model_validate(data)
    except ValidationError as exc:
        raise AnalyticsEventValidationError(str(exc)) from exc


def event_to_dict(event: AnalyticsEventPayload) -> dict[str, Any]:
    """Serialize a validated event for storage or transport."""
    return event.model_dump(mode="json")
