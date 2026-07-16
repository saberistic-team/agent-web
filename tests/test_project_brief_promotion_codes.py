"""Tests for Stripe promotion codes on Project Brief checkout (#197)."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Generator
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app import db, stripe_service
from app.main import app

client = TestClient(app)

SAMPLE_BRIEF = {
    "website": "https://example.com",
    "email": "client@example.com",
    "brief": "We need a security review of our API.",
}

FULL_PRICE_SESSION: dict[str, Any] = {
    "id": "cs_test_full",
    "payment_intent": "pi_test_full",
    "metadata": {"brief_id": "1"},
    "amount_subtotal": 20_000,
    "amount_total": 20_000,
    "currency": "usd",
    "total_details": {"amount_discount": 0},
    "discounts": [],
}

DISCOUNTED_SESSION: dict[str, Any] = {
    "id": "cs_test_discounted",
    "payment_intent": "pi_test_discounted",
    "metadata": {"brief_id": "2"},
    "amount_subtotal": 20_000,
    "amount_total": 15_000,
    "currency": "usd",
    "total_details": {"amount_discount": 5_000},
    "discounts": [
        {
            "promotion_code": {"id": "promo_test_25off"},
            "coupon": {"id": "coupon_test_25off"},
        }
    ],
}

FREE_SESSION: dict[str, Any] = {
    "id": "cs_test_free",
    "payment_intent": None,
    "metadata": {"brief_id": "3"},
    "amount_subtotal": 20_000,
    "amount_total": 0,
    "currency": "usd",
    "total_details": {"amount_discount": 20_000},
    "discounts": [
        {
            "promotion_code": {"id": "promo_test_free"},
            "coupon": {"id": "coupon_test_free"},
        }
    ],
}


@pytest.fixture(autouse=True)
def brief_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fake")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test_fake")
    monkeypatch.setenv("RESEND_API_KEY", "re_test_fake")
    monkeypatch.setenv("BASE_URL", "http://testserver")


@contextmanager
def mock_db_connection() -> Generator[MagicMock, None, None]:
    conn = MagicMock()
    with patch("app.main.db.db_connection") as db_conn:
        db_conn.return_value.__enter__.return_value = conn
        db_conn.return_value.__exit__.return_value = None
        yield conn


def _paid_brief_from_session(session: dict[str, Any]) -> dict[str, Any]:
    payment = stripe_service.extract_payment_details_from_session(session)
    return {
        "id": int(session["metadata"]["brief_id"]),
        "status": "paid",
        "payment_amount_cents": payment["payment_amount_cents"],
        "payment_subtotal_cents": payment["payment_subtotal_cents"],
        "payment_discount_cents": payment["payment_discount_cents"],
        "payment_currency": payment["payment_currency"],
        "stripe_promotion_code_id": payment["stripe_promotion_code_id"],
        "stripe_coupon_id": payment["stripe_coupon_id"],
        "utm_source": None,
        "utm_medium": None,
        "utm_campaign": None,
        "utm_content": None,
        "utm_term": None,
    }


@pytest.mark.unit
def test_create_checkout_session_enables_promotion_codes() -> None:
    fake = MagicMock()
    with patch("app.stripe_service.stripe.checkout.Session.create", return_value=fake) as create:
        stripe_service.create_checkout_session(
            secret_key="sk_test",
            brief_id=1,
            website="https://acme.dev",
            base_url="http://localhost:8000",
            price_cents=20_000,
        )
    assert create.call_args.kwargs["allow_promotion_codes"] is True


@pytest.mark.unit
def test_extract_payment_details_from_discounted_session() -> None:
    details = stripe_service.extract_payment_details_from_session(DISCOUNTED_SESSION)
    assert details["payment_subtotal_cents"] == 20_000
    assert details["payment_discount_cents"] == 5_000
    assert details["payment_amount_cents"] == 15_000
    assert details["payment_currency"] == "usd"
    assert details["stripe_promotion_code_id"] == "promo_test_25off"
    assert details["stripe_coupon_id"] == "coupon_test_25off"


@pytest.mark.unit
def test_extract_payment_details_from_free_session() -> None:
    details = stripe_service.extract_payment_details_from_session(FREE_SESSION)
    assert details["payment_amount_cents"] == 0
    assert details["payment_discount_cents"] == 20_000
    assert details["stripe_promotion_code_id"] == "promo_test_free"


@pytest.mark.unit
@pytest.mark.integration
def test_stripe_webhook_full_price_persists_payment_totals() -> None:
    fake_event = {"type": "checkout.session.completed", "data": {"object": FULL_PRICE_SESSION}}
    paid_row = _paid_brief_from_session(FULL_PRICE_SESSION)

    with mock_db_connection() as conn:
        with patch("app.main.stripe_service.construct_webhook_event", return_value=fake_event):
            with patch("app.main.db.mark_brief_paid", return_value=paid_row) as mark_paid:
                with patch("app.main.server_analytics.record_payment_completed") as track_payment:
                    response = client.post(
                        "/webhooks/stripe",
                        content=b"{}",
                        headers={"stripe-signature": "sig_test"},
                    )

    assert response.status_code == 200
    mark_paid.assert_called_once()
    kwargs = mark_paid.call_args.kwargs
    assert kwargs["brief_id"] == 1
    assert kwargs["payment_amount_cents"] == 20_000
    assert kwargs["payment_discount_cents"] is None
    track_payment.assert_called_once()
    assert track_payment.call_args.kwargs["price_cents"] == 20_000


@pytest.mark.unit
@pytest.mark.integration
def test_stripe_webhook_discounted_persists_payment_totals_and_analytics() -> None:
    fake_event = {
        "type": "checkout.session.completed",
        "data": {"object": DISCOUNTED_SESSION},
    }
    paid_row = _paid_brief_from_session(DISCOUNTED_SESSION)

    with mock_db_connection() as conn:
        with patch("app.main.stripe_service.construct_webhook_event", return_value=fake_event):
            with patch("app.main.db.mark_brief_paid", return_value=paid_row) as mark_paid:
                with patch("app.main.server_analytics.record_payment_completed") as track_payment:
                    response = client.post(
                        "/webhooks/stripe",
                        content=b"{}",
                        headers={"stripe-signature": "sig_test"},
                    )

    assert response.status_code == 200
    kwargs = mark_paid.call_args.kwargs
    assert kwargs["payment_amount_cents"] == 15_000
    assert kwargs["payment_discount_cents"] == 5_000
    assert kwargs["stripe_promotion_code_id"] == "promo_test_25off"
    assert track_payment.call_args.kwargs["price_cents"] == 15_000


@pytest.mark.unit
@pytest.mark.integration
def test_stripe_webhook_free_session_without_payment_intent() -> None:
    fake_event = {"type": "checkout.session.completed", "data": {"object": FREE_SESSION}}
    paid_row = _paid_brief_from_session(FREE_SESSION)

    with mock_db_connection():
        with patch("app.main.stripe_service.construct_webhook_event", return_value=fake_event):
            with patch("app.main.db.mark_brief_paid", return_value=paid_row) as mark_paid:
                with patch("app.main.server_analytics.record_payment_completed") as track_payment:
                    response = client.post(
                        "/webhooks/stripe",
                        content=b"{}",
                        headers={"stripe-signature": "sig_test"},
                    )

    assert response.status_code == 200
    assert mark_paid.call_args.kwargs["stripe_payment_intent_id"] is None
    assert mark_paid.call_args.kwargs["payment_amount_cents"] == 0
    assert track_payment.call_args.kwargs["price_cents"] == 0


@pytest.mark.unit
@pytest.mark.integration
def test_stripe_webhook_duplicate_event_is_idempotent() -> None:
    fake_event = {
        "type": "checkout.session.completed",
        "data": {"object": DISCOUNTED_SESSION},
    }
    with mock_db_connection():
        with patch("app.main.stripe_service.construct_webhook_event", return_value=fake_event):
            with patch("app.main.db.mark_brief_paid", return_value=None) as mark_paid:
                with patch("app.main.email_service.notify_team_of_paid_brief") as notify:
                    response = client.post(
                        "/webhooks/stripe",
                        content=b"{}",
                        headers={"stripe-signature": "sig_test"},
                    )
    assert response.status_code == 200
    mark_paid.assert_called_once()
    notify.assert_not_called()


@pytest.mark.unit
def test_mark_brief_paid_sql_includes_payment_columns() -> None:
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cur.fetchone.return_value = {"id": 1, "status": "paid", "payment_amount_cents": 15_000}

    db.mark_brief_paid(
        conn,
        brief_id=1,
        stripe_session_id="cs_test",
        stripe_payment_intent_id="pi_test",
        payment_subtotal_cents=20_000,
        payment_discount_cents=5_000,
        payment_amount_cents=15_000,
        payment_currency="usd",
        stripe_promotion_code_id="promo_test",
        stripe_coupon_id="coupon_test",
    )

    sql = " ".join(str(cur.execute.call_args[0][0]).split())
    assert "payment_subtotal_cents" in sql
    assert "payment_discount_cents" in sql
    assert "payment_amount_cents" in sql
    assert "stripe_promotion_code_id" in sql


@pytest.mark.unit
def test_project_brief_payment_migration_is_idempotent() -> None:
    from app.migrations.definitions import MIGRATIONS

    migration = next(m for m in MIGRATIONS if m.name == "project_brief_payment_details")
    assert migration.version == "016"
    for column in (
        "payment_subtotal_cents",
        "payment_discount_cents",
        "payment_amount_cents",
        "payment_currency",
        "stripe_promotion_code_id",
        "stripe_coupon_id",
    ):
        assert f"ADD COLUMN IF NOT EXISTS {column}" in migration.up_sql


@pytest.mark.unit
def test_render_admin_brief_detail_shows_discounted_payment_breakdown() -> None:
    from datetime import datetime, timezone

    from app.admin_pages import render_admin_brief_detail_page
    from app.brief_service import BriefListFilters

    brief = {
        "id": 9,
        "created_at": datetime(2026, 7, 14, 10, 30, tzinfo=timezone.utc),
        "website": "https://acme.example",
        "contact_method": "email",
        "contact_value": "ops@acme.example",
        "brief": "Need architecture review.",
        "status": "paid",
        "stripe_session_id": "cs_test",
        "stripe_payment_intent_id": "pi_test",
        "paid_at": datetime(2026, 7, 14, 10, 45, tzinfo=timezone.utc),
        "payment_subtotal_cents": 20_000,
        "payment_discount_cents": 5_000,
        "payment_amount_cents": 15_000,
        "payment_currency": "usd",
        "stripe_promotion_code_id": "promo_test",
        "stripe_coupon_id": "coupon_test",
        "utm_source": None,
        "utm_medium": None,
        "utm_campaign": None,
        "utm_content": None,
        "utm_term": None,
    }
    html_out = render_admin_brief_detail_page(
        admin_username="operator",
        brief=brief,
        back_filters=BriefListFilters(
            page=1,
            per_page=50,
            query=None,
            status=None,
            date_from=None,
            date_to=None,
            date_from_raw=None,
            date_to_raw=None,
        ),
        price_cents=20_000,
    )
    assert "Subtotal: $200 USD" in html_out
    assert "Discount: −$50 USD" in html_out
    assert "Total: $150 USD" in html_out
    assert "promo_test" in html_out
    assert "coupon_test" in html_out
