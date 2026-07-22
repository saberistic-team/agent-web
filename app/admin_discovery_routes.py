"""Lead discovery review inbox admin routes."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError

from app import db
from app.actor_context import actor_context_from_request
from app.admin_discovery_pages import (
    render_discovery_bulk_preview_page,
    render_discovery_candidate_page,
    render_discovery_inbox_page,
)
from app.config import get_settings
from app.discovery_inbox import (
    DiscoveryBulkLimitError,
    DiscoveryCandidateAccept,
    DiscoveryCandidateDefer,
    DiscoveryCandidateNotFoundError,
    DiscoveryCandidateReject,
    DiscoveryCandidateStateError,
    DiscoveryInboxError,
    DiscoveryInboxFilters,
)
from app.discovery_inbox_service import DiscoveryInboxService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/discovery", tags=["admin-discovery"])
_inbox = DiscoveryInboxService()


def _parse_filters(
    *,
    source: str | None = None,
    run_id: str | None = None,
    category: str | None = None,
    confidence: str | None = None,
    freshness: str | None = None,
    review_state: str | None = None,
) -> tuple[DiscoveryInboxFilters | None, dict[str, str | None]]:
    raw = {
        "source": source,
        "run_id": run_id,
        "category": category,
        "confidence": confidence,
        "freshness": freshness,
        "review_state": review_state or "pending",
    }
    try:
        filters = DiscoveryInboxFilters.model_validate(raw)
    except ValidationError:
        filters = DiscoveryInboxFilters()
        raw = {key: None if key != "review_state" else "pending" for key in raw}
    has_filter = any(value for key, value in raw.items() if key != "review_state" or value != "pending")
    if raw.get("review_state") and raw.get("review_state") != "pending":
        has_filter = True
    return (filters if has_filter else DiscoveryInboxFilters(), raw)


def _parse_deferred_until(raw: str | None) -> datetime:
    if not raw or not raw.strip():
        raise DiscoveryInboxError("deferred_until is required")
    parsed = datetime.fromisoformat(raw.strip())
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def admin_discovery_inbox(
    request: Request,
    source: str | None = None,
    run_id: str | None = None,
    category: str | None = None,
    confidence: str | None = None,
    freshness: str | None = None,
    review_state: str | None = None,
    saved: str | None = None,
) -> HTMLResponse:
    from app.admin_routes import _session_csrf_for_forms, require_admin_session

    session = require_admin_session(request)
    settings = get_settings()
    csrf_token = _session_csrf_for_forms(request, settings)
    filters, filter_values = _parse_filters(
        source=source,
        run_id=run_id,
        category=category,
        confidence=confidence,
        freshness=freshness,
        review_state=review_state,
    )

    if settings.admin_preview_enabled:
        from app.admin_preview import (
            build_preview_discovery_filter_metadata,
            build_preview_discovery_inbox,
        )

        return HTMLResponse(
            render_discovery_inbox_page(
                candidates=build_preview_discovery_inbox(filters=filter_values),
                filters=filter_values,
                filter_metadata=build_preview_discovery_filter_metadata(),
                csrf_token=csrf_token,
                admin_username=session.admin_username,
                preview_banner="Preview data — not production",
                status_message="Bulk action completed." if saved else None,
            )
        )

    candidates: list[dict] = []
    metadata: dict = {"sources": [], "runs": []}
    if settings.database_url:
        try:
            with db.db_connection(settings.database_url) as conn:
                candidates = _inbox.list_candidates(conn, filters=filters)
                metadata = _inbox.list_filter_metadata(conn)
        except Exception:
            logger.exception("Failed to load discovery inbox")

    return HTMLResponse(
        render_discovery_inbox_page(
            candidates=candidates,
            filters=filter_values,
            filter_metadata=metadata,
            csrf_token=csrf_token,
            admin_username=session.admin_username,
            status_message="Bulk action completed." if saved else None,
        )
    )


@router.get("/bulk/preview", response_class=HTMLResponse)
def admin_discovery_bulk_preview_get(request: Request) -> RedirectResponse:
    from app.admin_routes import require_admin_session

    require_admin_session(request)
    return RedirectResponse(url="/admin/discovery", status_code=303)


@router.post("/bulk/preview", response_class=HTMLResponse)
def admin_discovery_bulk_preview_post(
    request: Request,
    csrf_token: str = Form(...),
    action: str = Form(...),
    candidate_ids: list[str] = Form(default=[]),
    rejection_reason: str | None = Form(default=None),
    deferred_until: str | None = Form(default=None),
) -> HTMLResponse:
    from app.admin_routes import _session_csrf_for_forms, _verify_session_csrf, require_admin_session

    session = require_admin_session(request)
    settings = get_settings()
    _verify_session_csrf(request, session, csrf_token)
    form_csrf = _session_csrf_for_forms(request, settings)

    if settings.admin_preview_enabled:
        from app.admin_preview import build_preview_discovery_bulk_preview

        preview = build_preview_discovery_bulk_preview(action=action, candidate_ids=candidate_ids)
        return HTMLResponse(
            render_discovery_bulk_preview_page(
                preview=preview,
                csrf_token=form_csrf,
                admin_username=session.admin_username,
                preview_banner="Preview data — not production",
            )
        )

    if not settings.database_url:
        raise HTTPException(status_code=503, detail="Database is not configured.")

    try:
        parsed_ids = [UUID(value) for value in candidate_ids]
        deferred = _parse_deferred_until(deferred_until) if deferred_until else None
        with db.db_connection(settings.database_url) as conn:
            preview = _inbox.preview_bulk_action(
                conn,
                action=action,  # type: ignore[arg-type]
                candidate_ids=parsed_ids,
                rejection_reason=rejection_reason,
                deferred_until=deferred,
            )
    except DiscoveryBulkLimitError as exc:
        return HTMLResponse(
            render_discovery_inbox_page(
                candidates=[],
                filters={"review_state": "pending"},
                filter_metadata={"sources": [], "runs": []},
                csrf_token=form_csrf,
                admin_username=session.admin_username,
                error_message=str(exc),
            ),
            status_code=400,
        )
    except (DiscoveryCandidateNotFoundError, DiscoveryInboxError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return HTMLResponse(
        render_discovery_bulk_preview_page(
            preview=preview,
            csrf_token=form_csrf,
            admin_username=session.admin_username,
        )
    )


@router.post("/bulk/commit", response_class=HTMLResponse)
def admin_discovery_bulk_commit(
    request: Request,
    csrf_token: str = Form(...),
    action: str = Form(...),
    preview_token: str = Form(...),
    candidate_ids: list[str] = Form(default=[]),
    rejection_reason: str | None = Form(default=None),
    deferred_until: str | None = Form(default=None),
) -> RedirectResponse:
    from app.admin_routes import _verify_session_csrf, require_admin_session

    session = require_admin_session(request)
    settings = get_settings()
    _verify_session_csrf(request, session, csrf_token)

    if settings.admin_preview_enabled:
        raise HTTPException(status_code=405, detail="Bulk commit is disabled in preview mode.")

    if not settings.database_url:
        raise HTTPException(status_code=503, detail="Database is not configured.")

    actor = actor_context_from_request(request, actor=session.admin_username)
    parsed_ids = [UUID(value) for value in candidate_ids]
    deferred = _parse_deferred_until(deferred_until) if deferred_until else None
    try:
        with db.db_connection(settings.database_url) as conn:
            _inbox.commit_bulk_action(
                conn,
                actor_context=actor,
                action=action,  # type: ignore[arg-type]
                candidate_ids=parsed_ids,
                preview_token=preview_token,
                rejection_reason=rejection_reason,
                deferred_until=deferred,
            )
    except (DiscoveryBulkLimitError, DiscoveryInboxError, DiscoveryCandidateStateError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return RedirectResponse(url="/admin/discovery?saved=1", status_code=303)


@router.get("/{candidate_id}", response_class=HTMLResponse)
def admin_discovery_candidate_detail(request: Request, candidate_id: UUID) -> HTMLResponse:
    from app.admin_routes import _session_csrf_for_forms, require_admin_session

    session = require_admin_session(request)
    settings = get_settings()
    csrf_token = _session_csrf_for_forms(request, settings)

    if settings.admin_preview_enabled:
        from app.admin_preview import (
            build_preview_discovery_candidate_detail,
            preview_discovery_candidate_exists,
        )

        if not preview_discovery_candidate_exists(candidate_id):
            raise HTTPException(status_code=404, detail="Candidate not found")
        candidate = build_preview_discovery_candidate_detail(candidate_id)
        return HTMLResponse(
            render_discovery_candidate_page(
                candidate=candidate,
                csrf_token=csrf_token,
                admin_username=session.admin_username,
                preview_banner="Preview data — not production",
            )
        )

    if not settings.database_url:
        raise HTTPException(status_code=503, detail="Database is not configured.")

    with db.db_connection(settings.database_url) as conn:
        candidate = _inbox.get_candidate_detail(conn, candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail="Candidate not found")
    return HTMLResponse(
        render_discovery_candidate_page(
            candidate=candidate,
            csrf_token=csrf_token,
            admin_username=session.admin_username,
        )
    )


@router.post("/{candidate_id}/accept", response_class=HTMLResponse)
def admin_discovery_accept_candidate(
    request: Request,
    candidate_id: UUID,
    csrf_token: str = Form(...),
    company_choice: str = Form(...),
    selected_company_id: str | None = Form(default=None),
) -> RedirectResponse:
    from app.admin_routes import _verify_session_csrf, require_admin_session

    session = require_admin_session(request)
    settings = get_settings()
    _verify_session_csrf(request, session, csrf_token)

    if settings.admin_preview_enabled:
        raise HTTPException(status_code=405, detail="Accept is disabled in preview mode.")

    if not settings.database_url:
        raise HTTPException(status_code=503, detail="Database is not configured.")

    try:
        payload = DiscoveryCandidateAccept.model_validate(
            {
                "company_choice": company_choice,
                "selected_company_id": selected_company_id or None,
            }
        )
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    actor = actor_context_from_request(request, actor=session.admin_username)
    company_id = UUID(payload.selected_company_id) if payload.selected_company_id else None
    try:
        with db.db_connection(settings.database_url) as conn:
            result = _inbox.accept_candidate(
                conn,
                candidate_id=candidate_id,
                actor_context=actor,
                company_choice=payload.company_choice,
                selected_company_id=company_id,
            )
    except (DiscoveryCandidateNotFoundError, DiscoveryCandidateStateError, DiscoveryInboxError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    company = result["company"]
    return RedirectResponse(
        url=f"/admin/companies/{company['id']}",
        status_code=303,
    )


@router.post("/{candidate_id}/reject", response_class=HTMLResponse)
def admin_discovery_reject_candidate(
    request: Request,
    candidate_id: UUID,
    csrf_token: str = Form(...),
    rejection_reason: str = Form(...),
) -> RedirectResponse:
    from app.admin_routes import _verify_session_csrf, require_admin_session

    session = require_admin_session(request)
    settings = get_settings()
    _verify_session_csrf(request, session, csrf_token)

    if settings.admin_preview_enabled:
        raise HTTPException(status_code=405, detail="Reject is disabled in preview mode.")

    if not settings.database_url:
        raise HTTPException(status_code=503, detail="Database is not configured.")

    try:
        payload = DiscoveryCandidateReject.model_validate({"rejection_reason": rejection_reason})
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    actor = actor_context_from_request(request, actor=session.admin_username)
    try:
        with db.db_connection(settings.database_url) as conn:
            _inbox.reject_candidate(
                conn,
                candidate_id=candidate_id,
                actor_context=actor,
                rejection_reason=payload.rejection_reason,
            )
    except (DiscoveryCandidateNotFoundError, DiscoveryCandidateStateError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return RedirectResponse(url="/admin/discovery", status_code=303)


@router.post("/{candidate_id}/defer", response_class=HTMLResponse)
def admin_discovery_defer_candidate(
    request: Request,
    candidate_id: UUID,
    csrf_token: str = Form(...),
    deferred_until: str = Form(...),
) -> RedirectResponse:
    from app.admin_routes import _verify_session_csrf, require_admin_session

    session = require_admin_session(request)
    settings = get_settings()
    _verify_session_csrf(request, session, csrf_token)

    if settings.admin_preview_enabled:
        raise HTTPException(status_code=405, detail="Defer is disabled in preview mode.")

    if not settings.database_url:
        raise HTTPException(status_code=503, detail="Database is not configured.")

    try:
        parsed_until = _parse_deferred_until(deferred_until)
        payload = DiscoveryCandidateDefer.model_validate({"deferred_until": parsed_until})
    except (ValidationError, DiscoveryInboxError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    actor = actor_context_from_request(request, actor=session.admin_username)
    try:
        with db.db_connection(settings.database_url) as conn:
            _inbox.defer_candidate(
                conn,
                candidate_id=candidate_id,
                actor_context=actor,
                deferred_until=payload.deferred_until,
            )
    except (DiscoveryCandidateNotFoundError, DiscoveryCandidateStateError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return RedirectResponse(url="/admin/discovery", status_code=303)
