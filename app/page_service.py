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
    """Inject first-party analytics tags when ingestion is enabled."""
    injection = ""
    if settings.first_party_analytics_enabled:
        injection += '    <meta name="saberistic-first-party-analytics" content="1">\n'
        if page_event:
            event_esc = html.escape(page_event, quote=True)
            injection += (
                '    <meta name="saberistic-first-party-page-event" '
                f'content="{event_esc}">\n'
            )
        if case_study_slug:
            slug_esc = html.escape(case_study_slug, quote=True)
            injection += (
                '    <meta name="saberistic-first-party-case-study-slug" '
                f'content="{slug_esc}">\n'
            )
        if article_slug:
            slug_esc = html.escape(article_slug, quote=True)
            injection += (
                '    <meta name="saberistic-first-party-article-slug" '
                f'content="{slug_esc}">\n'
            )
        if 'src="/assets/first_party_analytics.js"' not in page_html:
            injection += (
                '    <script src="/assets/first_party_analytics.js" defer></script>\n'
            )

    if injection:
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
