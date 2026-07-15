"""Minimal hello-world HTTP API + saberistic landing site."""

from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import analytics_service, case_studies, db, email_service, insights, page_service, stripe_service
from app.admin_auth import AdminLoginRequired, login_redirect_url
from app.admin_pipeline_routes import router as admin_pipeline_router
from app.admin_routes import router as admin_router
from app.actor_context import CORRELATION_HEADER
from app.client_source import admin_proxy_trust_summary, client_source_policy_summary
from app.admin_security import validate_admin_auth_security_settings
from app.config import get_settings
from app.models import BriefCreateRequest, BriefCreateResponse
from app.seo import (
    PERMANENT_REDIRECTS,
    apex_redirect_url,
    is_www_host,
    robots_txt,
    sitemap_xml,
    wants_json_not_found,
)

logger = logging.getLogger(__name__)

SITE_DIR = Path(__file__).resolve().parent.parent / "site"
ASSETS_DIR = SITE_DIR / "assets"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    if settings.admin_auth_configured:
        validate_admin_auth_security_settings(settings)
    if settings.database_configured:
        db.init_db(settings.database_url)
        logger.info("database schema ready")
    else:
        logger.warning("DATABASE_URL not set — brief persistence disabled")
    yield


app = FastAPI(title="agent-web", version="0.3.0", lifespan=lifespan)
app.mount("/assets", StaticFiles(directory=ASSETS_DIR), name="assets")
app.include_router(admin_pipeline_router)
app.include_router(admin_router)


@app.exception_handler(AdminLoginRequired)
async def redirect_unauthenticated_admin(
    request: Request,
    exc: AdminLoginRequired,
) -> RedirectResponse:
    return RedirectResponse(url=login_redirect_url(exc.next_path), status_code=303)


@app.middleware("http")
async def attach_correlation_id(request: Request, call_next):
    correlation_id = request.headers.get(CORRELATION_HEADER, "").strip()
    if not correlation_id:
        correlation_id = str(uuid.uuid4())
    request.state.correlation_id = correlation_id
    response = await call_next(request)
    response.headers[CORRELATION_HEADER] = correlation_id
    return response


@app.middleware("http")
async def redirect_www_to_apex(request: Request, call_next):
    host = request.headers.get("host", "")
    if is_www_host(host):
        target = apex_redirect_url(request.url.path, request.url.query)
        return RedirectResponse(url=target, status_code=301)
    return await call_next(request)


@app.exception_handler(StarletteHTTPException)
async def handle_http_exception(request: Request, exc: StarletteHTTPException) -> Response:
    if exc.status_code != 404:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    if wants_json_not_found(request.url.path, request.headers.get("accept", "")):
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    response = page_service.serve_page("404.html", get_settings())
    response.status_code = 404
    return response


@app.get("/robots.txt", response_class=PlainTextResponse)
def robots() -> str:
    return robots_txt()


@app.get("/sitemap.xml")
def sitemap() -> Response:
    return Response(content=sitemap_xml(), media_type="application/xml")


@app.get("/health")
def health() -> dict:
    """Process liveness. Optionally reports ``schema_version`` when the DB is readable.

    Migrations run at startup (``db.init_db``). If they fail, uvicorn never serves
    this path and Render marks the deploy ``update_failed``. ``schema_version`` is
    best-effort for post-deploy verification — connection errors must not turn
    liveness into 503 (that breaks readiness probes and unit tests that set a
    unused DATABASE_URL).
    """
    payload: dict = {"status": "ok"}
    settings = get_settings()
    payload["admin_client_source_policy"] = client_source_policy_summary(settings)
    payload["admin_proxy_trust"] = admin_proxy_trust_summary(settings)
    if not settings.database_configured:
        return payload
    try:
        version = db.latest_schema_version(settings.database_url)
    except Exception:
        logger.exception("health: failed to read schema_migrations")
        return payload
    if version is not None:
        payload["schema_version"] = version
    return payload


@app.get("/hello")
def hello() -> dict[str, str]:
    return {"message": "hello world"}


@app.get("/")
def home() -> HTMLResponse:
    return page_service.serve_page("index.html", get_settings())


@app.get("/about")
def about() -> HTMLResponse:
    return page_service.serve_page("about.html", get_settings())


@app.get("/services")
def services() -> HTMLResponse:
    return page_service.serve_page("services.html", get_settings())


@app.get("/case-studies")
def case_studies_index() -> HTMLResponse:
    return page_service.serve_page("case-studies.html", get_settings())


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
    return page_service.serve_html(
        case_studies.render_case_study_page(study),
        get_settings(),
        page_event="Case Study Viewed",
        case_study_slug=study["slug"],
    )


@app.get("/insights")
def insights_index() -> HTMLResponse:
    return page_service.serve_html(insights.render_insights_index(), get_settings())


@app.get("/insights/feed.xml")
def insights_feed() -> Response:
    return Response(content=insights.render_atom_feed(), media_type="application/atom+xml")


@app.get("/insights/{slug}")
def insight_article(slug: str) -> HTMLResponse:
    article = insights.get_insight(slug)
    if article is None:
        raise HTTPException(status_code=404, detail="Insight not found")
    return page_service.serve_html(
        insights.render_insight_page(article),
        get_settings(),
        page_event="Insight Viewed",
        article_slug=article["slug"],
    )


for redirect_path, target in PERMANENT_REDIRECTS.items():

    def _permanent_redirect(
        _target: str = target,
    ) -> RedirectResponse:
        return RedirectResponse(url=_target, status_code=301)

    app.add_api_route(
        redirect_path, _permanent_redirect, methods=["GET"], include_in_schema=False
    )


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
        payment_details = stripe_service.extract_payment_details_from_session(session)
        paid_brief = db.mark_brief_paid(
            conn,
            brief_id=brief_id,
            stripe_session_id=session.get("id"),
            stripe_payment_intent_id=session.get("payment_intent"),
            **payment_details,
        )

    if paid_brief is None:
        return JSONResponse({"received": True})

    paid_amount_cents = paid_brief.get("payment_amount_cents")
    if paid_amount_cents is None:
        paid_amount_cents = payment_details.get("payment_amount_cents")
    if paid_amount_cents is None:
        paid_amount_cents = settings.brief_price_cents

    try:
        analytics_service.track_payment_completed(
            settings,
            brief_id=brief_id,
            price_cents=int(paid_amount_cents),
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
