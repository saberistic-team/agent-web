"""Serve static site pages with optional analytics injection."""

from __future__ import annotations

import html
import re
from pathlib import Path

from fastapi.responses import HTMLResponse

from app.config import Settings
from app.site_layout import PAGE_PATH_BY_FILENAME, render_site_header

SITE_DIR = Path(__file__).resolve().parent.parent / "site"

_HEADER_PATTERN = re.compile(r"<header class=\"top\">.*?</header>", re.DOTALL)


def inject_site_header(page_html: str, active_path: str | None) -> str:
    """Replace the page header with the shared primary navigation."""
    header = render_site_header(active_path)
    return _HEADER_PATTERN.sub(header, page_html, count=1)


def inject_analytics(
    page_html: str,
    settings: Settings,
    *,
    page_event: str | None = None,
    case_study_slug: str | None = None,
    article_slug: str | None = None,
) -> str:
    """Inject Plausible analytics tags when analytics is enabled."""
    if settings.analytics_enabled and settings.plausible_domain:
        domain = html.escape(settings.plausible_domain, quote=True)
        injection = (
            f'    <meta name="saberistic-analytics-domain" content="{domain}">\n'
        )
        if page_event:
            event_esc = html.escape(page_event, quote=True)
            injection += (
                f'    <meta name="saberistic-analytics-page-event" content="{event_esc}">\n'
            )
        if case_study_slug:
            slug_esc = html.escape(case_study_slug, quote=True)
            injection += (
                '    <meta name="saberistic-analytics-case-study-slug" '
                f'content="{slug_esc}">\n'
            )
        if article_slug:
            slug_esc = html.escape(article_slug, quote=True)
            injection += (
                f'    <meta name="saberistic-analytics-article-slug" content="{slug_esc}">\n'
            )
        injection += '    <script src="/assets/analytics.js" defer></script>\n'
        if 'src="/assets/analytics.js"' not in page_html:
            page_html = page_html.replace("</head>", f"{injection}  </head>", 1)
    return page_html


def serve_html(
    page_html: str,
    settings: Settings,
    *,
    page_event: str | None = None,
    case_study_slug: str | None = None,
    article_slug: str | None = None,
) -> HTMLResponse:
    """Return HTML with optional analytics injection."""
    return HTMLResponse(
        content=inject_analytics(
            page_html,
            settings,
            page_event=page_event,
            case_study_slug=case_study_slug,
            article_slug=article_slug,
        )
    )


def serve_page(filename: str, settings: Settings) -> HTMLResponse:
    """Return a site HTML page, injecting analytics tags when enabled."""
    page_html = (SITE_DIR / filename).read_text(encoding="utf-8")
    active_path = PAGE_PATH_BY_FILENAME.get(filename)
    if active_path is not None or filename in PAGE_PATH_BY_FILENAME:
        page_html = inject_site_header(page_html, active_path)
    return serve_html(page_html, settings)
