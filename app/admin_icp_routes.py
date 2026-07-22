"""ICP scoring admin routes."""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError

from app import db
from app.admin_icp_pages import (
    render_icp_rules_page,
    render_icp_score_detail_page,
    render_icp_scores_list_page,
)
from app.actor_context import actor_context_from_request
from app.config import get_settings
from app.crm_service import CrmService
from app.icp_scoring import IcpRuleThreshold, IcpScoringRule, default_icp_rules, rule_from_row

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/signals", tags=["admin-icp"])
_crm = CrmService()


def _parse_rules_form(form: dict[str, str], existing_rules: list[dict]) -> list[IcpScoringRule]:
    existing_by_id = {str(row["id"]): rule_from_row(row) for row in existing_rules}
    parsed: list[IcpScoringRule] = []
    for rule_id, existing in existing_by_id.items():
        threshold = existing.threshold.model_dump()
        keywords_raw = form.get(f"keywords__{rule_id}", "").strip()
        if keywords_raw:
            threshold["keywords"] = [
                part.strip() for part in keywords_raw.split(",") if part.strip()
            ]
        max_days_raw = form.get(f"max_days__{rule_id}", "").strip()
        if max_days_raw:
            threshold["max_days"] = int(max_days_raw)
        elif "max_days" in threshold and not max_days_raw:
            threshold.pop("max_days", None)
        parsed.append(
            IcpScoringRule(
                id=rule_id,
                dimension=form.get(f"dimension__{rule_id}", existing.dimension),
                label=form.get(f"label__{rule_id}", existing.label).strip() or existing.label,
                weight=float(form.get(f"weight__{rule_id}", existing.weight)),
                threshold=IcpRuleThreshold.model_validate(threshold),
                enabled=f"enabled__{rule_id}" in form,
                accept_hypothesis=f"accept_hypothesis__{rule_id}" in form,
                sort_order=existing.sort_order,
            )
        )
    return sorted(parsed, key=lambda rule: (rule.sort_order, rule.id))


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def admin_icp_scores_list(request: Request) -> HTMLResponse:
    from app.admin_routes import _session_csrf_for_forms, require_admin_session

    session = require_admin_session(request)
    settings = get_settings()
    csrf_token = _session_csrf_for_forms(request, settings)

    if settings.admin_preview_enabled:
        from app.admin_preview import build_preview_icp_score_rows, build_preview_icp_version

        return HTMLResponse(
            render_icp_scores_list_page(
                rows=build_preview_icp_score_rows(),
                active_version=build_preview_icp_version(),
                csrf_token=csrf_token,
                admin_username=session.admin_username,
                preview_banner="Preview data — not production",
            )
        )

    rows: list[dict] = []
    active_version = None
    if settings.database_url:
        try:
            with db.db_connection(settings.database_url) as conn:
                rows = _crm.list_company_icp_scores(conn)
                active_version = _crm.get_active_icp_version(conn)
        except Exception:
            logger.exception("Failed to load ICP scores")

    return HTMLResponse(
        render_icp_scores_list_page(
            rows=rows,
            active_version=active_version,
            csrf_token=csrf_token,
            admin_username=session.admin_username,
        )
    )


@router.get("/rules", response_class=HTMLResponse)
def admin_icp_rules(request: Request, error: str | None = None) -> HTMLResponse:
    from app.admin_routes import _session_csrf_for_forms, require_admin_session

    session = require_admin_session(request)
    settings = get_settings()
    csrf_token = _session_csrf_for_forms(request, settings)

    if settings.admin_preview_enabled:
        from app.admin_preview import build_preview_icp_rules, build_preview_icp_version

        return HTMLResponse(
            render_icp_rules_page(
                rules=build_preview_icp_rules(),
                active_version=build_preview_icp_version(),
                csrf_token=csrf_token,
                admin_username=session.admin_username,
                preview_banner="Preview data — not production",
                error_message=error,
            )
        )

    rules: list[dict] = []
    active_version = None
    if settings.database_url:
        try:
            with db.db_connection(settings.database_url) as conn:
                active_version = _crm.get_active_icp_version(conn)
                rules = _crm.list_active_icp_rules(conn)
        except Exception:
            logger.exception("Failed to load ICP rules")

    if not rules:
        rules = [rule.model_dump() for rule in default_icp_rules()]

    return HTMLResponse(
        render_icp_rules_page(
            rules=rules,
            active_version=active_version,
            csrf_token=csrf_token,
            admin_username=session.admin_username,
            error_message=error,
        )
    )


@router.post("/rules")
async def admin_icp_rules_save(
    request: Request,
    csrf_token: str = Form(...),
) -> RedirectResponse:
    from app.admin_routes import (
        _session_csrf_for_forms,
        _verify_session_csrf,
        require_admin_session,
    )

    session = require_admin_session(request)
    settings = get_settings()
    _verify_session_csrf(request, session, csrf_token)

    if not settings.database_url:
        raise HTTPException(status_code=503, detail="Database not configured")

    form_data = await request.form()
    form = {key: str(value) for key, value in form_data.multi_items()}
    actor = actor_context_from_request(request, actor=session.admin_username)
    try:
        with db.db_connection(settings.database_url) as conn:
            existing_rules = _crm.list_active_icp_rules(conn)
            if not existing_rules:
                existing_rules = [rule.model_dump() for rule in default_icp_rules()]
            rules = _parse_rules_form(form, existing_rules)
            _crm.publish_icp_rule_version(
                conn,
                actor_context=actor,
                rules=rules,
            )
    except (ValueError, ValidationError) as exc:
        return RedirectResponse(
            url=f"/admin/signals/rules?error={str(exc)}",
            status_code=303,
        )
    except Exception:
        logger.exception("Failed to publish ICP rule version")
        raise HTTPException(status_code=500, detail="Rule update failed") from None

    return RedirectResponse(url="/admin/signals/rules", status_code=303)


@router.get("/{company_id}", response_class=HTMLResponse)
def admin_icp_score_detail(
    request: Request,
    company_id: UUID,
    error: str | None = None,
) -> HTMLResponse:
    from app.admin_routes import _session_csrf_for_forms, require_admin_session

    session = require_admin_session(request)
    settings = get_settings()
    csrf_token = _session_csrf_for_forms(request, settings)

    if settings.admin_preview_enabled:
        from app.admin_preview import build_preview_icp_score_detail

        detail = build_preview_icp_score_detail(company_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="Company not found")
        return HTMLResponse(
            render_icp_score_detail_page(
                company=detail["company"],
                snapshot=detail["snapshot"],
                active_version=detail["active_version"],
                csrf_token=csrf_token,
                admin_username=session.admin_username,
                preview_banner="Preview data — not production",
                error_message=error,
            )
        )

    if not settings.database_url:
        raise HTTPException(status_code=503, detail="Database not configured")

    with db.db_connection(settings.database_url) as conn:
        detail = _crm.get_company_icp_score_detail(conn, company_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="Company not found")

    return HTMLResponse(
        render_icp_score_detail_page(
            company=detail["company"],
            snapshot=detail["snapshot"],
            active_version=detail["active_version"],
            csrf_token=csrf_token,
            admin_username=session.admin_username,
            error_message=error,
        )
    )


@router.post("/{company_id}/recalculate")
def admin_icp_recalculate(
    request: Request,
    company_id: UUID,
    csrf_token: str = Form(...),
) -> RedirectResponse:
    from app.admin_routes import (
        _session_csrf_for_forms,
        _verify_session_csrf,
        require_admin_session,
    )

    session = require_admin_session(request)
    settings = get_settings()
    _verify_session_csrf(request, session, csrf_token)

    if not settings.database_url:
        raise HTTPException(status_code=503, detail="Database not configured")

    actor = actor_context_from_request(request, actor=session.admin_username)
    try:
        with db.db_connection(settings.database_url) as conn:
            _crm.calculate_company_icp_score(
                conn,
                actor_context=actor,
                company_id=company_id,
            )
    except ValueError as exc:
        return RedirectResponse(
            url=f"/admin/signals/{company_id}?error={str(exc)}",
            status_code=303,
        )
    except Exception:
        logger.exception("Failed to recalculate ICP score")
        raise HTTPException(status_code=500, detail="Recalculation failed") from None

    return RedirectResponse(url=f"/admin/signals/{company_id}", status_code=303)


@router.post("/{company_id}/override")
def admin_icp_override(
    request: Request,
    company_id: UUID,
    csrf_token: str = Form(...),
    override_score: float = Form(...),
    reason: str = Form(...),
) -> RedirectResponse:
    from app.admin_routes import (
        _session_csrf_for_forms,
        _verify_session_csrf,
        require_admin_session,
    )

    session = require_admin_session(request)
    settings = get_settings()
    _verify_session_csrf(request, session, csrf_token)

    if not settings.database_url:
        raise HTTPException(status_code=503, detail="Database not configured")

    actor = actor_context_from_request(request, actor=session.admin_username)
    try:
        with db.db_connection(settings.database_url) as conn:
            _crm.override_company_icp_score(
                conn,
                actor_context=actor,
                company_id=company_id,
                override_score=override_score,
                reason=reason,
            )
    except ValueError as exc:
        return RedirectResponse(
            url=f"/admin/signals/{company_id}?error={str(exc)}",
            status_code=303,
        )
    except Exception:
        logger.exception("Failed to override ICP score")
        raise HTTPException(status_code=500, detail="Override failed") from None

    return RedirectResponse(url=f"/admin/signals/{company_id}", status_code=303)
