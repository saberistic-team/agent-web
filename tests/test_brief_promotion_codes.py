"""Tests for Stripe promotion codes on Project Brief checkout (#197)."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Generator
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app import db as brief_db
from app import stripe_service
from app.main import app

client = TestClient(app)

SAMPLE_BRIEF = {
    "website": "https://example.com",
    "email": "client@example.com",
    "brief": "We need a security review of our API.",
}

FAKE_PAID_BRIEF: dict[str, Any] = {
    "id": 1,
    "website": SAMPLE_BRIEF["website"],
    "contact_method": "email",
    "contact_value": SAMPLE_BRIEF["email"],
    "brief": SAMPLE_BRIEF["brief"],
    "status": "paid",
    "stripe_session_id": "cs_test_123",
    "stripe_payment_intent_id": "pi_test_123",
    "paid_at": "2026-07-11T00:00:00+00:00",
    "payment_subtotal_cents": 20_000,
    "payment_discount_cents": None,
    "payment_amount_cents": 20_000,
    "payment_currency": "usd",
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


def _completed_session(
    *,
    amount_subtotal: int = 20_000,
    amount_total: int = 20_000,
    amount_discount: int = 0,
    payment_intent: str | None = "pi_test_123",
    promotion_code_id: str | None = None,
    coupon_id: str | None = None,
) -> dict[str, Any]:
    discounts: list[dict[str, Any]] = []
    if promotion_code_id or coupon_id:
        discounts.append(
            {
                "promotion_code": {"id": promotion_code_id} if promotion_code_id else None,
                "coupon": {"id": coupon_id} if coupon_id else None,
            }
        )
    return {
        "id": "cs_test_123",
        "payment_intent": payment_intent,
        "metadata": {"brief_id": "1"},
        "amount_subtotal": amount_subtotal,
        "amount_total": amount_total,
        "currency": "usd",
        "total_details": {"amount_discount": amount_discount},
        "discounts": discounts,
    }


@pytest.mark.unit
def test_extract_payment_details_full_price() -> None:
    payment = stripe_service.extract_payment_details_from_session(_completed_session())
    assert payment["payment_subtotal_cents"] == 20_000
    assert payment["payment_discount_cents"] is None
    assert payment["payment_amount_cents"] == 20_000
    assert payment["payment_currency"] == "usd"
    assert payment["stripe_promotion_code_id"] is None
    assert payment["stripe_coupon_id"] is None


@pytest.mark.unit
def test_extract_payment_details_discounted() -> None:
    payment = stripe_service.extract_payment_details_from_session(
        _completed_session(
            amount_total=15_000,
            amount_discount=5_000,
            promotion_code_id="promo_abc",
            coupon_id="coupon_xyz",
        )
    )
    assert payment["payment_subtotal_cents"] == 20_000
    assert payment["payment_discount_cents"] == 5_000
    assert payment["payment_amount_cents"] == 15_000
    assert payment["stripe_promotion_code_id"] == "promo_abc"
    assert payment["stripe_coupon_id"] == "coupon_xyz"


@pytest.mark.unit
def test_extract_payment_details_hundred_percent_off() -> None:
    payment = stripe_service.extract_payment_details_from_session(
        _completed_session(
            amount_total=0,
            amount_discount=20_000,
            payment_intent=None,
            promotion_code_id="promo_free",
            coupon_id="coupon_free",
        )
    )
    assert payment["payment_amount_cents"] == 0
    assert payment["payment_discount_cents"] == 20_000


@pytest.mark.unit
@pytest.mark.integration
def test_stripe_webhook_full_price_persists_payment_totals() -> None:
    fake_event = {
        "type": "checkout.session.completed",
        "data": {"object": _completed_session()},
    }

    with mock_db_connection() as conn:
        with patch(
            "app.main.stripe_service.construct_webhook_event",
            return_value=fake_event,
        ):
            with patch(
                "app.main.db.mark_brief_paid",
                return_value=FAKE_PAID_BRIEF,
            ) as mark_paid:
                response = client.post(
                    "/webhooks/stripe",
                    content=b"{}",
                    headers={"stripe-signature": "sig_test"},
                )

    assert response.status_code == 200
    mark_paid.assert_called_once_with(
        conn,
        brief_id=1,
        stripe_session_id="cs_test_123",
        stripe_payment_intent_id="pi_test_123",
        payment_subtotal_cents=20_000,
        payment_discount_cents=None,
        payment_amount_cents=20_000,
        payment_currency="usd",
        stripe_promotion_code_id=None,
        stripe_coupon_id=None,
    )


@pytest.mark.unit
@pytest.mark.integration
def test_stripe_webhook_discounted_persists_payment_totals() -> None:
    fake_event = {
        "type": "checkout.session.completed",
        "data": {
            "object": _completed_session(
                amount_total=15_000,
                amount_discount=5_000,
                promotion_code_id="promo_abc",
                coupon_id="coupon_xyz",
            )
        },
    }
    discounted_brief = {
        **FAKE_PAID_BRIEF,
        "payment_discount_cents": 5_000,
        "payment_amount_cents": 15_000,
        "stripe_promotion_code_id": "promo_abc",
        "stripe_coupon_id": "coupon_xyz",
    }

    with mock_db_connection() as conn:
        with patch(
            "app.main.stripe_service.construct_webhook_event",
            return_value=fake_event,
        ):
            with patch(
                "app.main.db.mark_brief_paid",
                return_value=discounted_brief,
            ) as mark_paid:
                with patch(
                    "app.main.analytics_service.track_payment_completed"
                ) as track_payment:
                    response = client.post(
                        "/webhooks/stripe",
                        content=b"{}",
                        headers={"stripe-signature": "sig_test"},
                    )

    assert response.status_code == 200
    mark_paid.assert_called_once_with(
        conn,
        brief_id=1,
        stripe_session_id="cs_test_123",
        stripe_payment_intent_id="pi_test_123",
        payment_subtotal_cents=20_000,
        payment_discount_cents=5_000,
        payment_amount_cents=15_000,
        payment_currency="usd",
        stripe_promotion_code_id="promo_abc",
        stripe_coupon_id="coupon_xyz",
    )
    track_payment.assert_called_once()
    assert track_payment.call_args.kwargs["price_cents"] == 15_000


@pytest.mark.unit
@pytest.mark.integration
def test_stripe_webhook_hundred_percent_off_without_payment_intent() -> None:
    fake_event = {
        "type": "checkout.session.completed",
        "data": {
            "object": _completed_session(
                amount_total=0,
                amount_discount=20_000,
                payment_intent=None,
                promotion_code_id="promo_free",
                coupon_id="coupon_free",
            )
        },
    }
    free_brief = {
        **FAKE_PAID_BRIEF,
        "stripe_payment_intent_id": None,
        "payment_discount_cents": 20_000,
        "payment_amount_cents": 0,
        "stripe_promotion_code_id": "promo_free",
        "stripe_coupon_id": "coupon_free",
    }

    with mock_db_connection() as conn:
        with patch(
            "app.main.stripe_service.construct_webhook_event",
            return_value=fake_event,
        ):
            with patch(
                "app.main.db.mark_brief_paid",
                return_value=free_brief,
            ) as mark_paid:
                response = client.post(
                    "/webhooks/stripe",
                    content=b"{}",
                    headers={"stripe-signature": "sig_test"},
                )

    assert response.status_code == 200
    mark_paid.assert_called_once_with(
        conn,
        brief_id=1,
        stripe_session_id="cs_test_123",
        stripe_payment_intent_id=None,
        payment_subtotal_cents=20_000,
        payment_discount_cents=20_000,
        payment_amount_cents=0,
        payment_currency="usd",
        stripe_promotion_code_id="promo_free",
        stripe_coupon_id="coupon_free",
    )


@pytest.mark.unit
@pytest.mark.integration
def test_stripe_webhook_duplicate_event_is_idempotent() -> None:
    fake_event = {
        "type": "checkout.session.completed",
        "data": {"object": _completed_session()},
    }

    with mock_db_connection():
        with patch(
            "app.main.stripe_service.construct_webhook_event",
            return_value=fake_event,
        ):
            with patch("app.main.db.mark_brief_paid", return_value=None):
                with patch(
                    "app.main.analytics_service.track_payment_completed"
                ) as track_payment:
                    with patch(
                        "app.main.email_service.notify_team_of_paid_brief"
                    ) as notify_team:
                        response = client.post(
                            "/webhooks/stripe",
                            content=b"{}",
                            headers={"stripe-signature": "sig_test"},
                        )

    assert response.status_code == 200
    track_payment.assert_not_called()
    notify_team.assert_not_called()


@pytest.mark.unit
@pytest.mark.integration
def test_abandoned_checkout_does_not_mark_paid() -> None:
    """Invalid or unused promotion codes never complete checkout — no webhook."""
    fake_session = MagicMock()
    fake_session.id = "cs_test_abandoned"
    fake_session.url = "https://checkout.stripe.com/c/pay/cs_test_abandoned"

    with mock_db_connection() as conn:
        with patch("app.main.db.create_brief", return_value=7):
            with patch("app.main.db.update_brief_stripe_session"):
                with patch(
                    "app.main.stripe_service.create_checkout_session",
                    return_value=fake_session,
                ):
                    with patch("app.main.db.mark_brief_paid") as mark_paid:
                        response = client.post("/api/briefs", json=SAMPLE_BRIEF)

        pending_row: dict[str, Any] = {
            "id": 7,
            "status": "pending_payment",
            "payment_amount_cents": None,
            "payment_discount_cents": None,
        }
        with patch("app.db.get_brief_by_id", return_value=pending_row):
            stored = brief_db.get_brief_by_id(conn, 7)

    assert response.status_code == 200
    mark_paid.assert_not_called()
    assert stored is not None
    assert stored["status"] == "pending_payment"
    assert stored["payment_amount_cents"] is None


@pytest.mark.unit
def test_project_briefs_payment_details_migration_is_idempotent() -> None:
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
def test_existing_paid_rows_keep_nullable_payment_fields() -> None:
    legacy_paid = {
        "id": 9,
        "status": "paid",
        "payment_subtotal_cents": None,
        "payment_discount_cents": None,
        "payment_amount_cents": None,
        "payment_currency": None,
        "stripe_promotion_code_id": None,
        "stripe_coupon_id": None,
    }
    from app.admin_pages import _brief_paid_amount_cents, _brief_payment_summary_lines

    assert _brief_paid_amount_cents(legacy_paid, list_price_cents=20_000) == 20_000
    assert _brief_payment_summary_lines(legacy_paid, list_price_cents=20_000) == ["$200"]
