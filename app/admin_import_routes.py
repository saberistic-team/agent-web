"""Admin routes for LinkedIn export import preview."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.admin_import_pages import render_linkedin_import_page
from app.config import get_settings

router = APIRouter(prefix="/admin/imports", tags=["admin-imports"])


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def admin_imports_page(request: Request) -> HTMLResponse:
    from app.admin_routes import (
        _session_csrf_for_forms,
        require_admin_session,
    )

    session = require_admin_session(request)
    settings = get_settings()
    csrf_token = _session_csrf_for_forms(request, settings)

    if settings.admin_preview_enabled:
        from app.admin_preview import build_preview_linkedin_import

        return HTMLResponse(
            render_linkedin_import_page(
                admin_username=session.admin_username,
                csrf_token=csrf_token,
                preview_banner="Preview data — not production",
                preview_data=build_preview_linkedin_import(),
                include_scripts=False,
            )
        )

    return HTMLResponse(
        render_linkedin_import_page(
            admin_username=session.admin_username,
            csrf_token=csrf_token,
        )
    )
