"""Serve static site pages with optional analytics injection."""

from __future__ import annotations

import html
from pathlib import Path

from fastapi.responses import HTMLResponse

from app.config import Settings

SITE_DIR = Path(__file__).resolve().parent.parent / "site"


def inject_analytics(page_html: str, settings: Settings) -> str:
    """Inject Plausible analytics tags when enabled."""
    if settings.analytics_enabled and settings.plausible_domain:
        domain = html.escape(settings.plausible_domain, quote=True)
        injection = (
            f'    <meta name="saberistic-analytics-domain" content="{domain}">\n'
            '    <script src="/assets/analytics.js" defer></script>\n'
        )
        if 'src="/assets/analytics.js"' not in page_html:
            page_html = page_html.replace("</head>", f"{injection}  </head>", 1)
    return page_html


def serve_page(filename: str, settings: Settings) -> HTMLResponse:
    """Return a site HTML page, injecting analytics tags when enabled."""
    page_html = (SITE_DIR / filename).read_text(encoding="utf-8")
    return HTMLResponse(content=inject_analytics(page_html, settings))
