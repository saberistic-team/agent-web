"""Minimal hello-world HTTP API + saberistic landing site."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import analytics_service, case_studies, db, email_service, page_service, stripe_service
from app.config import get_settings
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
def home() -> HTMLResponse:
    return page_service.serve_page("index.html", get_settings())


@app.get("/about")
def about() -> HTMLResponse:
    return page_service.serve_page("about.html", get_settings())


@app.get("/brief")
def brief_form() -> HTMLResponse:
    return page_service.serve_page("brief.html", get_settings())


@app.get("/brief/success")
def brief_success() -> HTMLResponse:
    return page_service.serve_page("brief-success.html", get_settings())


@app.get("/work/{slug}")
def case_study(slug: str) -> HTMLResponse:
    study = case_studies.get_case_study(slug)
    if study is None:
        raise HTTPException(status_code=404, detail="Case study not found")
    return HTMLResponse(case_studies.render_case_study_page(study))


@app.post("/api/briefs", response_model=BriefCreateResponse)
def create_brief(payload: BriefCreateRequest) -> BriefCreateResponse:
    settings = get_settings()
    if not settings.database_configured:
        raise HTTPException(status_code=503, detail="Database not configured")
    if not settings.stripe_configured:
        raise HTTPException(status_code=503, detail="Stripe not configured")

    with db.db_connection(settings.database_url) as conn:
        utm = payload.utm_attribution()
        brief_id = db.create_brief(
            conn,
            website=payload.website,
            contact_method="email",
            contact_value=payload.email,
            brief=payload.brief,
            utm_source=utm["utm_source"],
            utm_medium=utm["utm_medium"],
            utm_campaign=utm["utm_campaign"],
            utm_content=utm["utm_content"],
            utm_term=utm["utm_term"],
        )

        try:
            analytics_service.track_lead_persisted(
                settings,
                brief_id=brief_id,
                utm=utm,
            )
        except Exception:
            logger.exception("Analytics lead_persisted failed for brief %s", brief_id)

        # Lead emails before Stripe so a checkout failure still notifies inbox.
        if settings.email_configured:
            try:
                email_service.notify_team_of_new_brief(
                    api_key=settings.resend_api_key,
                    from_email=settings.from_email,
                    notify_email=settings.notify_email,
                    brief_id=brief_id,
                    website=payload.website,
                    email=payload.email,
                    brief=payload.brief,
                )
                email_service.notify_customer_of_brief_received(
                    api_key=settings.resend_api_key,
                    from_email=settings.from_email,
                    to_email=payload.email,
                    website=payload.website,
                )
            except Exception:
                logger.exception("Failed to send brief lead emails for %s", brief_id)

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

    try:
        analytics_service.track_checkout_opened(
            settings,
            brief_id=brief_id,
            price_cents=settings.brief_price_cents,
            utm=utm,
        )
    except Exception:
        logger.exception("Analytics checkout_opened failed for brief %s", brief_id)

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

    try:
        analytics_service.track_payment_completed(
            settings,
            brief_id=brief_id,
            price_cents=settings.brief_price_cents,
            utm={
                "utm_source": paid_brief.get("utm_source"),
                "utm_medium": paid_brief.get("utm_medium"),
                "utm_campaign": paid_brief.get("utm_campaign"),
                "utm_content": paid_brief.get("utm_content"),
                "utm_term": paid_brief.get("utm_term"),
            },
        )
    except Exception:
        logger.exception("Analytics payment_completed failed for brief %s", brief_id)

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
