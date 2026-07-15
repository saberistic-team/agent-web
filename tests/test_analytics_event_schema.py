"""Tests for versioned first-party analytics event schema (#113)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app import analytics_event_schema as schema

VALID_SESSION = "550e8400-e29b-41d4-a716-446655440000"
UTC = timezone.utc


def _occurred() -> datetime:
    return datetime(2026, 7, 15, 12, 0, 0, tzinfo=UTC)


@pytest.mark.unit
def test_schema_version_is_semver_string() -> None:
    assert schema.SCHEMA_VERSION == "1.0.0"


@pytest.mark.unit
def test_all_documented_events_are_allowlisted() -> None:
    expected = {
        "Landing Viewed",
        "About Viewed",
        "Services Viewed",
        "Case Studies Viewed",
        "Case Study Viewed",
        "Insights Viewed",
        "Insight Viewed",
        "Nav Services",
        "Nav Case Studies",
        "Nav Insights",
        "Nav Diagnostic",
        "Brief Viewed",
        "Brief Form Started",
        "Lead Persisted",
        "Checkout Opened",
        "Payment Completed",
        "Contact Initiated",
        "Checkout Cancelled",
        "Brief Success Viewed",
    }
    assert expected == schema.ALLOWED_EVENT_NAMES


@pytest.mark.unit
def test_engagement_and_conversion_sets_are_disjoint() -> None:
    overlap = schema.ENGAGEMENT_EVENT_NAMES & schema.CONVERSION_EVENT_NAMES
    assert not overlap
    assert (
        schema.ENGAGEMENT_EVENT_NAMES | schema.CONVERSION_EVENT_NAMES
        == schema.ALLOWED_EVENT_NAMES
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("pathname", "expected"),
    [
        ("/", schema.PathClass.LANDING),
        ("/about", schema.PathClass.ABOUT),
        ("/about/", schema.PathClass.ABOUT),
        ("/services", schema.PathClass.SERVICES),
        ("/case-studies", schema.PathClass.CASE_STUDIES),
        ("/work/brave", schema.PathClass.CASE_STUDY),
        ("/insights", schema.PathClass.INSIGHTS),
        ("/insights/mvp-competing-sources-of-truth", schema.PathClass.INSIGHT),
        ("/brief", schema.PathClass.BRIEF),
        ("/brief/success", schema.PathClass.BRIEF_SUCCESS),
        ("/health", schema.PathClass.UNKNOWN),
        (None, schema.PathClass.UNKNOWN),
    ],
)
def test_classify_path(pathname: str | None, expected: schema.PathClass) -> None:
    assert schema.classify_path(pathname) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("host", "site_host", "expected"),
    [
        (None, None, schema.ReferrerClass.DIRECT),
        ("", None, schema.ReferrerClass.DIRECT),
        ("saberistic.com", "saberistic.com", schema.ReferrerClass.INTERNAL),
        ("www.google.com", None, schema.ReferrerClass.SEARCH),
        ("www.linkedin.com", None, schema.ReferrerClass.SOCIAL),
        ("mail.google.com", None, schema.ReferrerClass.EMAIL),
        ("unknown.example", None, schema.ReferrerClass.UNKNOWN_EXTERNAL),
    ],
)
def test_classify_referrer(
    host: str | None,
    site_host: str | None,
    expected: schema.ReferrerClass,
) -> None:
    assert schema.classify_referrer(host, site_host=site_host) == expected


@pytest.mark.unit
def test_anonymous_session_id_requires_uuid() -> None:
    assert schema.is_valid_anonymous_session_id(VALID_SESSION)
    assert not schema.is_valid_anonymous_session_id("not-a-uuid")
    assert not schema.is_valid_anonymous_session_id("")


@pytest.mark.unit
def test_build_event_payload_accepts_valid_lead_persisted() -> None:
    event = schema.build_event_payload(
        event_name=schema.EVENT_LEAD_PERSISTED,
        anonymous_session_id=VALID_SESSION,
        pathname="/brief",
        occurred_at=_occurred(),
        properties={
            "brief_id": 42,
            "funnel_step": 5,
            "linkage_source": "server_brief_persist",
            "environment": "production",
        },
        attribution={"utm_source": "linkedin", "utm_medium": "social"},
    )
    assert event.event_name == schema.EVENT_LEAD_PERSISTED
    assert event.schema_version == "1.0.0"
    assert event.path_class == schema.PathClass.BRIEF
    assert event.linkage_state == schema.LinkageState.CRM_BRIEF_LINKED
    assert event.properties["brief_id"] == 42
    assert event.attribution == {"utm_source": "linkedin", "utm_medium": "social"}


@pytest.mark.unit
def test_build_event_payload_accepts_engagement_page_event() -> None:
    event = schema.build_event_payload(
        event_name=schema.EVENT_SERVICES_VIEWED,
        anonymous_session_id=VALID_SESSION,
        pathname="/services",
        properties={"page": "/services"},
    )
    assert event.path_class == schema.PathClass.SERVICES
    assert event.linkage_state == schema.LinkageState.ANONYMOUS
    assert "funnel_step" not in event.properties


@pytest.mark.unit
def test_build_event_payload_accepts_nav_event() -> None:
    event = schema.build_event_payload(
        event_name=schema.EVENT_NAV_DIAGNOSTIC,
        anonymous_session_id=VALID_SESSION,
        pathname="/",
        properties={"page": "/", "nav_destination": "/brief"},
    )
    assert event.event_name == schema.EVENT_NAV_DIAGNOSTIC
    assert event.properties["nav_destination"] == "/brief"


@pytest.mark.unit
def test_build_event_payload_accepts_case_study_viewed() -> None:
    event = schema.build_event_payload(
        event_name=schema.EVENT_CASE_STUDY_VIEWED,
        anonymous_session_id=VALID_SESSION,
        pathname="/work/brave",
        properties={"page": "/work/brave", "case_study_slug": "brave"},
    )
    assert event.properties["case_study_slug"] == "brave"


@pytest.mark.unit
def test_build_event_payload_accepts_contact_initiated() -> None:
    event = schema.build_event_payload(
        event_name=schema.EVENT_CONTACT_INITIATED,
        anonymous_session_id=VALID_SESSION,
        pathname="/about",
        properties={
            "page": "/about",
            "contact_channel": "linkedin",
            "funnel_step": 8,
        },
    )
    assert event.properties["contact_channel"] == "linkedin"


@pytest.mark.unit
def test_rejects_unknown_event_name() -> None:
    with pytest.raises(schema.AnalyticsEventValidationError, match="Unknown event name"):
        schema.build_event_payload(
            event_name="Mystery Click",
            anonymous_session_id=VALID_SESSION,
            pathname="/",
        )


@pytest.mark.unit
def test_rejects_disallowed_property() -> None:
    with pytest.raises(schema.AnalyticsEventValidationError, match="allowlist"):
        schema.sanitize_properties(
            {"brief_id": 1, "custom_metric": "nope"},
            event_name=schema.EVENT_LEAD_PERSISTED,
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    "sensitive",
    [
        {"email": "user@example.com"},
        {"phone": "+15551234567"},
        {"brief": "secret scope"},
        {"wallet_address": "0xabc"},
        {"stripe_session_id": "cs_live_x"},
        {"query_string": "utm_source=x"},
        {"user_agent": "Mozilla/5.0"},
        {"fingerprint": "abc123"},
        {"ip_address": "203.0.113.1"},
    ],
)
def test_rejects_sensitive_property_keys(sensitive: dict[str, str]) -> None:
    with pytest.raises(schema.AnalyticsEventValidationError, match="Sensitive"):
        schema.sanitize_properties(
            {**{"brief_id": 1, "funnel_step": 5}, **sensitive},
            event_name=schema.EVENT_LEAD_PERSISTED,
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    "value",
    [
        "client@example.com",
        "https://evil.example/path",
        "http://tracking.example",
    ],
)
def test_rejects_sensitive_property_values(value: str) -> None:
    with pytest.raises(schema.AnalyticsEventValidationError, match="Sensitive value"):
        schema.sanitize_properties(
            {"page": value},
            event_name=schema.EVENT_LANDING_VIEWED,
        )


@pytest.mark.unit
def test_rejects_missing_required_properties_for_server_event() -> None:
    with pytest.raises(schema.AnalyticsEventValidationError, match="missing required"):
        schema.sanitize_properties(
            {"funnel_step": 5},
            event_name=schema.EVENT_LEAD_PERSISTED,
        )


@pytest.mark.unit
def test_rejects_forbidden_properties_on_nav_event() -> None:
    with pytest.raises(schema.AnalyticsEventValidationError, match="forbids"):
        schema.sanitize_properties(
            {"page": "/", "funnel_step": 1},
            event_name=schema.EVENT_NAV_SERVICES,
        )


@pytest.mark.unit
def test_rejects_invalid_funnel_step() -> None:
    with pytest.raises(schema.AnalyticsEventValidationError, match="Invalid funnel_step"):
        schema.sanitize_properties(
            {"page": "/brief", "funnel_step": 2},
            event_name=schema.EVENT_BRIEF_VIEWED,
        )


@pytest.mark.unit
def test_rejects_non_allowlisted_attribution_keys() -> None:
    with pytest.raises(ValidationError, match="Attribution keys not allowlisted"):
        schema.AnalyticsEventPayload(
            event_name=schema.EVENT_LANDING_VIEWED,
            occurred_at=_occurred(),
            anonymous_session_id=VALID_SESSION,
            path_class=schema.PathClass.LANDING,
            attribution={"gclid": "secret-click-id"},
            properties={"page": "/"},
        )


@pytest.mark.unit
def test_rejects_crm_linkage_without_brief_id() -> None:
    with pytest.raises(ValidationError, match="crm_brief_linked requires brief_id"):
        schema.AnalyticsEventPayload(
            event_name=schema.EVENT_LANDING_VIEWED,
            occurred_at=_occurred(),
            anonymous_session_id=VALID_SESSION,
            path_class=schema.PathClass.LANDING,
            linkage_state=schema.LinkageState.CRM_BRIEF_LINKED,
            properties={"page": "/"},
        )


@pytest.mark.unit
def test_parse_event_payload_round_trip() -> None:
    event = schema.build_event_payload(
        event_name=schema.EVENT_PAYMENT_COMPLETED,
        anonymous_session_id=VALID_SESSION,
        pathname="/brief/success",
        received_at=_occurred(),
        properties={
            "brief_id": 7,
            "price_cents": 20_000,
            "funnel_step": 7,
            "linkage_source": "server_payment_complete",
        },
    )
    raw = schema.event_to_dict(event)
    parsed = schema.parse_event_payload(raw)
    assert parsed.event_name == schema.EVENT_PAYMENT_COMPLETED
    assert parsed.received_at is not None
    assert parsed.properties["price_cents"] == 20_000


@pytest.mark.unit
def test_parse_event_payload_rejects_arbitrary_event() -> None:
    with pytest.raises(schema.AnalyticsEventValidationError):
        schema.parse_event_payload(
            {
                "event_name": "Random",
                "schema_version": "1.0.0",
                "occurred_at": _occurred().isoformat(),
                "anonymous_session_id": VALID_SESSION,
                "path_class": "landing",
                "referrer_class": "direct",
                "attribution": {},
                "properties": {"page": "/"},
                "consent_state": "implicit_analytics",
                "linkage_state": "anonymous",
            }
        )


@pytest.mark.unit
def test_filter_properties_lenient_drops_without_raising() -> None:
    result = schema.filter_properties(
        {
            "brief_id": 3,
            "email": "hidden@example.com",
            "unknown": "drop",
            "utm_source": "linkedin",
        }
    )
    assert result == {"brief_id": 3, "utm_source": "linkedin"}


@pytest.mark.unit
def test_anonymous_session_retention_constants_documented() -> None:
    assert schema.ANONYMOUS_SESSION_MAX_AGE_SECONDS == 86_400
    assert schema.ANONYMOUS_SESSION_ROTATION_SECONDS == 1_800


@pytest.mark.unit
def test_infer_linkage_state_anonymous_before_submit() -> None:
    state = schema.infer_linkage_state(
        event_name=schema.EVENT_BRIEF_FORM_STARTED,
        properties={"page": "/brief", "funnel_step": 4},
    )
    assert state == schema.LinkageState.ANONYMOUS


@pytest.mark.unit
def test_infer_linkage_state_linked_after_persist() -> None:
    state = schema.infer_linkage_state(
        event_name=schema.EVENT_LEAD_PERSISTED,
        properties={"brief_id": 10, "funnel_step": 5},
    )
    assert state == schema.LinkageState.CRM_BRIEF_LINKED
