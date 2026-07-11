"""Minimal hello-world HTTP API + saberistic landing site."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import db, email_service, stripe_service
from app.config import Settings, get_settings
from app.models import BriefCreateRequest, BriefCreateResponse

logger = logging.getLogger(__name__)

SITE_DIR = Path(__file__).resolve().parent.parent / "site"
ASSETS_DIR = SITE_DIR / "assets"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    if settings.database_configured:
        db.init_db(settings.database_url)
        logger.info("project_briefs table ready")
    else:
        logger.warning("DATABASE_URL not set — brief persistence disabled")
    yield


app = FastAPI(title="agent-web", version="0.3.0", lifespan=lifespan)
app.mount("/assets", StaticFiles(directory=ASSETS_DIR), name="assets")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/hello")
def hello() -> dict[str, str]:
    return {"message": "hello world"}


@app.get("/")
def home() -> FileResponse:
    return FileResponse(SITE_DIR / "index.html")


@app.get("/about")
def about() -> FileResponse:
    return FileResponse(SITE_DIR / "about.html")


@app.get("/brief")
def brief_form() -> FileResponse:
    return FileResponse(SITE_DIR / "brief.html")


@app.get("/brief/success")
def brief_success() -> FileResponse:
    return FileResponse(SITE_DIR / "brief-success.html")


@app.post("/api/briefs", response_model=BriefCreateResponse)
def create_brief(payload: BriefCreateRequest) -> BriefCreateResponse:
    settings = get_settings()
    if not settings.database_configured:
        raise HTTPException(status_code=503, detail="Database not configured")
    if not settings.stripe_configured:
        raise HTTPException(status_code=503, detail="Stripe not configured")

    with db.db_connection(settings.database_url) as conn:
        brief_id = db.create_brief(
            conn,
            website=payload.website,
            contact_method=payload.contact_method,
            contact_value=payload.contact_value,
            brief=payload.brief,
        )

        try:
            session = stripe_service.create_checkout_session(
                secret_key=settings.stripe_secret_key,
                brief_id=brief_id,
                website=payload.website,
                base_url=settings.base_url,
                price_cents=settings.brief_price_cents,
            )
        except Exception as exc:
            logger.exception("Stripe checkout session failed for brief %s", brief_id)
            raise HTTPException(status_code=502, detail="Payment session failed") from exc

        db.update_brief_stripe_session(
            conn,
            brief_id=brief_id,
            stripe_session_id=session.id,
        )

    if not session.url:
        raise HTTPException(status_code=502, detail="Payment session missing checkout URL")

    return BriefCreateResponse(checkout_url=session.url, brief_id=brief_id)


@app.post("/webhooks/stripe")
async def stripe_webhook(request: Request) -> JSONResponse:
    settings = get_settings()
    if not settings.stripe_webhook_secret:
        raise HTTPException(status_code=503, detail="Stripe webhook not configured")
    if not settings.database_configured:
        raise HTTPException(status_code=503, detail="Database not configured")

    payload = await request.body()
    signature = request.headers.get("stripe-signature", "")

    try:
        event = stripe_service.construct_webhook_event(
            payload=payload,
            signature=signature,
            webhook_secret=settings.stripe_webhook_secret,
        )
    except Exception as exc:
        logger.warning("Stripe webhook signature verification failed: %s", exc)
        raise HTTPException(status_code=400, detail="Invalid webhook signature") from exc

    if event["type"] != "checkout.session.completed":
        return JSONResponse({"received": True})

    session = event["data"]["object"]
    brief_id = stripe_service.extract_brief_id_from_session(session)
    if brief_id is None:
        logger.error("checkout.session.completed missing brief_id metadata")
        return JSONResponse({"received": True})

    with db.db_connection(settings.database_url) as conn:
        paid_brief = db.mark_brief_paid(
            conn,
            brief_id=brief_id,
            stripe_session_id=session.get("id"),
            stripe_payment_intent_id=session.get("payment_intent"),
        )

    if paid_brief is None:
        return JSONResponse({"received": True})

    if settings.email_configured:
        try:
            email_service.notify_team_of_paid_brief(
                api_key=settings.resend_api_key,
                from_email=settings.from_email,
                notify_email=settings.notify_email,
                brief=paid_brief,
            )
            email_service.notify_customer_of_paid_brief(
                api_key=settings.resend_api_key,
                from_email=settings.from_email,
                brief=paid_brief,
            )
        except Exception:
            logger.exception("Failed to send brief notification emails for %s", brief_id)

    return JSONResponse({"received": True})
