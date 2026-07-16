"""Tests for conversion funnel analytics (#66, #117 cutover)."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Generator
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app import analytics_service
from app.config import get_settings
from app.main import app

client = TestClient(app)

REPO_ROOT = Path(__file__).resolve().parent.parent

SAMPLE_BRIEF = {
    "website": "https://example.com",
    "email": "client@example.com",
    "brief": "We need a security review of our API.",
}

FAKE_PAID_BRIEF = {
    "id": 1,
    "website": SAMPLE_BRIEF["website"],
    "contact_method": "email",
    "contact_value": SAMPLE_BRIEF["email"],
    "brief": SAMPLE_BRIEF["brief"],
    "status": "paid",
    "stripe_session_id": "cs_test_123",
    "stripe_payment_intent_id": "pi_test_123",
    "paid_at": "2026-07-11T00:00:00+00:00",
    "utm_source": "linkedin",
    "utm_medium": "social",
    "utm_campaign": None,
    "utm_content": None,
    "utm_term": None,
}


@pytest.fixture(autouse=True)
def analytics_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fake")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test_fake")
    monkeypatch.setenv("RESEND_API_KEY", "re_test_fake")
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.delenv("FIRST_PARTY_ANALYTICS_ENABLED", raising=False)
    monkeypatch.delenv("ANALYTICS_ENABLED", raising=False)


@contextmanager
def mock_db_connection() -> Generator[MagicMock, None, None]:
    conn = MagicMock()
    with patch("app.main.db.db_connection") as db_conn:
        db_conn.return_value.__enter__.return_value = conn
        db_conn.return_value.__exit__.return_value = None
        yield conn


@pytest.mark.unit
def test_sanitize_properties_blocks_sensitive_fields() -> None:
    props = analytics_service.sanitize_properties(
        {
            "brief_id": 42,
            "email": "secret@example.com",
            "phone": "+15551234567",
            "wallet_address": "0xabc123",
            "website": "https://secret.com",
            "brief": "confidential scope",
            "stripe_session_id": "cs_live_secret",
            "checkout_url": "https://checkout.stripe.com/secret",
            "query_string": "utm_source=secret",
            "utm_source": "linkedin",
            "unknown_field": "drop me",
        }
    )
    assert props == {"brief_id": 42, "utm_source": "linkedin"}


@pytest.mark.unit
def test_sanitize_properties_allows_content_slugs() -> None:
    props = analytics_service.sanitize_properties(
        {
            "case_study_slug": "brave",
            "article_slug": "mvp-competing-sources-of-truth",
            "page": "/work/brave",
        }
    )
    assert props == {
        "case_study_slug": "brave",
        "article_slug": "mvp-competing-sources-of-truth",
        "page": "/work/brave",
    }


@pytest.mark.unit
def test_sanitize_properties_allowlist_only() -> None:
    props = analytics_service.sanitize_properties(
        {
            "brief_id": 1,
            "price_cents": 20_000,
            "funnel_step": 5,
            "environment": "production",
            "contact_channel": "linkedin",
        }
    )
    assert props["brief_id"] == 1
    assert props["price_cents"] == 20_000
    assert props["funnel_step"] == 5
    assert props["environment"] == "production"
    assert props["contact_channel"] == "linkedin"


@pytest.mark.unit
def test_analytics_disabled_without_explicit_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    assert not settings.first_party_analytics_enabled


@pytest.mark.unit
def test_analytics_enabled_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FIRST_PARTY_ANALYTICS_ENABLED", "true")
    monkeypatch.setenv("ANALYTICS_ENV", "production")
    settings = get_settings()
    assert settings.first_party_analytics_enabled
    assert settings.analytics_environment == "production"


@pytest.mark.unit
def test_analytics_enabled_legacy_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANALYTICS_ENABLED", "true")
    settings = get_settings()
    assert settings.first_party_analytics_enabled


@pytest.mark.unit
def test_track_event_noop_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    with patch("app.analytics_service.persist_analytics_event") as persist:
        analytics_service.track_event(settings, event_name="Lead Persisted", props={"brief_id": 1})
    persist.assert_not_called()


@pytest.mark.unit
def test_track_event_persists_sanitized_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FIRST_PARTY_ANALYTICS_ENABLED", "true")
    monkeypatch.setenv("ANALYTICS_ENV", "production")
    settings = get_settings()

    conn = MagicMock()
    with patch("app.analytics_service.db.db_connection") as db_conn:
        db_conn.return_value.__enter__.return_value = conn
        db_conn.return_value.__exit__.return_value = None
        with patch("app.analytics_service.persist_analytics_event", return_value=True) as persist:
            analytics_service.track_lead_persisted(
                settings,
                brief_id=9,
                utm={"utm_source": "linkedin", "utm_medium": None},
            )

    persist.assert_called_once()
    call_kwargs = persist.call_args.kwargs
    event = call_kwargs["event"]
    assert event.event_name == analytics_service.EVENT_LEAD_PERSISTED
    assert event.properties["brief_id"] == 9
    assert event.properties["funnel_step"] == 5
    assert event.properties["environment"] == "production"
    assert event.properties["linkage_source"] == "server_brief_persist"
    assert event.attribution["utm_source"] == "linkedin"
    assert "email" not in event.properties
    assert "website" not in event.properties


@pytest.mark.unit
def test_track_event_failure_is_non_blocking(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FIRST_PARTY_ANALYTICS_ENABLED", "true")
    settings = get_settings()

    with patch("app.analytics_service.db.db_connection") as db_conn:
        db_conn.side_effect = RuntimeError("database down")
        analytics_service.track_event(settings, event_name="Lead Persisted", props={"brief_id": 1})


@pytest.mark.unit
@pytest.mark.integration
def test_create_brief_emits_server_funnel_events(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FIRST_PARTY_ANALYTICS_ENABLED", "true")

    fake_session = MagicMock()
    fake_session.id = "cs_test_abc"
    fake_session.url = "https://checkout.stripe.com/c/pay/cs_test_abc"

    with mock_db_connection():
        with patch("app.main.db.create_brief", return_value=1):
            with patch("app.main.db.update_brief_stripe_session"):
                with patch(
                    "app.main.stripe_service.create_checkout_session",
                    return_value=fake_session,
                ):
                    with patch("app.main.analytics_service.track_lead_persisted") as track_lead:
                        with patch(
                            "app.main.analytics_service.track_checkout_opened"
                        ) as track_checkout:
                            response = client.post(
                                "/api/briefs",
                                json={
                                    **SAMPLE_BRIEF,
                                    "utm_source": "linkedin",
                                    "utm_medium": "social",
                                },
                            )

    assert response.status_code == 200
    track_lead.assert_called_once()
    assert track_lead.call_args.kwargs["brief_id"] == 1
    assert track_lead.call_args.kwargs["utm"]["utm_source"] == "linkedin"
    track_checkout.assert_called_once()
    assert track_checkout.call_args.kwargs["price_cents"] == 20_000


@pytest.mark.unit
@pytest.mark.integration
def test_analytics_failure_does_not_block_brief_create(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FIRST_PARTY_ANALYTICS_ENABLED", "true")

    fake_session = MagicMock()
    fake_session.id = "cs_test_abc"
    fake_session.url = "https://checkout.stripe.com/c/pay/cs_test_abc"

    with mock_db_connection():
        with patch("app.main.db.create_brief", return_value=1):
            with patch("app.main.db.update_brief_stripe_session"):
                with patch(
                    "app.main.stripe_service.create_checkout_session",
                    return_value=fake_session,
                ):
                    with patch(
                        "app.analytics_service.persist_analytics_event",
                        side_effect=RuntimeError("analytics down"),
                    ):
                        response = client.post("/api/briefs", json=SAMPLE_BRIEF)

    assert response.status_code == 200
    assert response.json()["checkout_url"] == fake_session.url


@pytest.mark.unit
@pytest.mark.integration
def test_stripe_webhook_emits_payment_completed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FIRST_PARTY_ANALYTICS_ENABLED", "true")

    fake_event = {
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_test_123",
                "payment_intent": "pi_test_123",
                "metadata": {"brief_id": "1"},
                "amount_subtotal": 20_000,
                "amount_total": 15_000,
                "currency": "usd",
                "total_details": {"amount_discount": 5_000},
            }
        },
    }

    with mock_db_connection():
        with patch(
            "app.main.stripe_service.construct_webhook_event",
            return_value=fake_event,
        ):
            with patch("app.main.db.mark_brief_paid", return_value=FAKE_PAID_BRIEF):
                with patch(
                    "app.main.analytics_service.track_payment_completed"
                ) as track_payment:
                    response = client.post(
                        "/webhooks/stripe",
                        content=b"{}",
                        headers={"stripe-signature": "sig_test"},
                    )

    assert response.status_code == 200
    track_payment.assert_called_once()
    assert track_payment.call_args.kwargs["brief_id"] == 1
    assert track_payment.call_args.kwargs["price_cents"] == 15_000
    assert track_payment.call_args.kwargs["utm"]["utm_source"] == "linkedin"


@pytest.mark.unit
def test_pages_inject_first_party_analytics_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FIRST_PARTY_ANALYTICS_ENABLED", "true")

    static_paths = (
        "/",
        "/about",
        "/services",
        "/case-studies",
        "/brief",
        "/brief/success",
        "/insights",
    )
    for path in static_paths:
        response = client.get(path)
        assert response.status_code == 200
        assert 'name="saberistic-first-party-analytics"' in response.text
        assert 'src="/assets/first_party_analytics.js"' in response.text
        assert 'name="saberistic-first-party-page-event"' not in response.text
        assert "plausible.io" not in response.text
        assert "saberistic-analytics-domain" not in response.text


@pytest.mark.unit
def test_case_study_page_injects_server_page_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FIRST_PARTY_ANALYTICS_ENABLED", "true")

    response = client.get("/work/brave")
    assert response.status_code == 200
    assert 'name="saberistic-first-party-page-event" content="Case Study Viewed"' in (
        response.text
    )
    assert 'name="saberistic-first-party-case-study-slug" content="brave"' in response.text
    assert "saberistic-first-party-article-slug" not in response.text


@pytest.mark.unit
def test_insight_article_injects_server_page_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FIRST_PARTY_ANALYTICS_ENABLED", "true")

    response = client.get("/insights/mvp-competing-sources-of-truth")
    assert response.status_code == 200
    assert 'name="saberistic-first-party-page-event" content="Insight Viewed"' in (
        response.text
    )
    assert (
        'name="saberistic-first-party-article-slug" '
        'content="mvp-competing-sources-of-truth"'
    ) in response.text
    assert "saberistic-first-party-case-study-slug" not in response.text


@pytest.mark.unit
def test_redirect_route_omits_page_event_meta(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FIRST_PARTY_ANALYTICS_ENABLED", "true")

    response = client.get("/diagnostic", follow_redirects=False)
    assert response.status_code == 301
    assert "saberistic-first-party-analytics" not in response.text


@pytest.mark.unit
def test_pages_omit_analytics_when_disabled() -> None:
    response = client.get("/brief")
    assert response.status_code == 200
    assert "saberistic-first-party-analytics" not in response.text
    assert 'src="/assets/first_party_analytics.js"' not in response.text
    assert "plausible.io" not in response.text


@pytest.mark.unit
def test_first_party_analytics_js_exists_and_documents_funnel() -> None:
    response = client.get("/assets/first_party_analytics.js")
    assert response.status_code == 200
    body = response.text
    assert "Brief Form Started" in body
    assert "Contact Initiated" in body
    assert "Nav Services" in body
    assert "Nav Case Studies" in body
    assert "Nav Insights" in body
    assert "Nav Diagnostic" in body
    assert "Services Viewed" in body
    assert "Case Studies Viewed" in body
    assert "saberistic_utm" in body
    assert "About Viewed" in body
    assert "saberistic-first-party-page-event" in body
    assert "saberistic-first-party-case-study-slug" in body
    assert "saberistic-first-party-article-slug" in body
    assert "Service Viewed" not in body
    assert '"/about": { event: "About Viewed"' in body
    assert '"/services": { event: "Services Viewed"' in body
    assert "plausible.io" not in body
    assert "window.plausible" not in body


@pytest.mark.unit
def test_legacy_plausible_analytics_js_removed() -> None:
    response = client.get("/assets/analytics.js")
    assert response.status_code == 404


@pytest.mark.unit
def test_first_party_analytics_js_content_pages_omit_funnel_step() -> None:
    body = client.get("/assets/first_party_analytics.js").text
    assert '"/about": { event: "About Viewed"' in body
    assert '"/services": { event: "Services Viewed"' in body
    assert '"/insights": { event: "Insights Viewed"' in body
    assert '"/brief": { event: "Brief Viewed", step: 3' in body


@pytest.mark.unit
def test_trailing_slash_pages_still_inject_analytics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FIRST_PARTY_ANALYTICS_ENABLED", "true")

    for path in ("/about/", "/services/", "/work/brave/"):
        response = client.get(path)
        assert response.status_code == 200
        assert 'name="saberistic-first-party-analytics"' in response.text
        assert 'src="/assets/first_party_analytics.js"' in response.text


@pytest.mark.unit
def test_brief_page_includes_utm_in_submit_script() -> None:
    response = client.get("/brief")
    assert "utm_source" in response.text
    assert "saberistic_utm" in response.text


@pytest.mark.unit
def test_no_plausible_references_in_app_or_site() -> None:
    """Post-cutover guard: no Plausible script, API, or env wiring remains."""
    forbidden = (
        "plausible.io",
        "PLAUSIBLE_DOMAIN",
        "PLAUSIBLE_API_KEY",
        "window.plausible",
        "saberistic-analytics-domain",
        'src="/assets/analytics.js"',
    )
    scan_roots = (
        REPO_ROOT / "app",
        REPO_ROOT / "site",
        REPO_ROOT / "render.yaml",
    )
    hits: list[str] = []
    for root in scan_roots:
        if root.is_file():
            text = root.read_text(encoding="utf-8")
            for needle in forbidden:
                if needle in text:
                    hits.append(f"{root}: {needle}")
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix not in {".py", ".js", ".html", ".yaml", ".md"}:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for needle in forbidden:
                if needle in text:
                    hits.append(f"{path.relative_to(REPO_ROOT)}: {needle}")
    assert hits == []
