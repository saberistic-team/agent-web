"""Shared layout fragments for the private admin interface."""

from __future__ import annotations

import html

ADMIN_NAV_LINKS: tuple[dict[str, str], ...] = (
    {
        "label": "Dashboard",
        "href": "/admin",
        "milestone": "CRM core",
        "summary": "Acquisition pipeline and daily attention queues",
    },
    {
        "label": "Audit",
        "href": "/admin/audit",
        "milestone": "Audit trail",
        "summary": "Immutable security and mutation history",
    },
    {
        "label": "Briefs",
        "href": "/admin/briefs",
        "milestone": "Brief intake",
        "summary": "Submitted project brief leads",
    },
    {
        "label": "Companies",
        "href": "/admin/companies",
        "milestone": "CRM data model",
        "summary": "Company records and firmographics",
    },
    {
        "label": "Contacts",
        "href": "/admin/contacts",
        "milestone": "CRM data model",
        "summary": "People, roles, and outreach history",
    },
    {
        "label": "Signals",
        "href": "/admin/signals",
        "milestone": "Signal intelligence",
        "summary": "Inbound triggers and intent scoring",
    },
    {
        "label": "Pipeline",
        "href": "/admin/pipeline",
        "milestone": "Pipeline operations",
        "summary": "Deal stages and follow-up queues",
    },
    {
        "label": "Imports",
        "href": "/admin/imports",
        "milestone": "Data import",
        "summary": "CSV and enrichment ingest",
    },
    {
        "label": "Discovery",
        "href": "/admin/discovery",
        "milestone": "Lead discovery",
        "summary": "Prospect search and list building",
    },
    {
        "label": "Analytics",
        "href": "/admin/analytics",
        "milestone": "Admin analytics",
        "summary": "Funnel metrics and attribution",
    },
    {
        "label": "Content",
        "href": "/admin/content",
        "milestone": "Content management",
        "summary": "Insights, case studies, and landing copy",
    },
    {
        "label": "Settings",
        "href": "/admin/settings",
        "milestone": "Admin settings",
        "summary": "Team access, integrations, and billing",
    },
)

ADMIN_PATHS: frozenset[str] = frozenset(link["href"] for link in ADMIN_NAV_LINKS)

# Pre-merge Playwright capture targets (shell pages + login). Never production.
ADMIN_SCREENSHOT_PATHS: tuple[str, ...] = (
    *(link["href"] for link in ADMIN_NAV_LINKS),
    "/admin/login",
    "/admin/briefs/1",
    "/admin/briefs/2",
    "/admin/briefs/3",
    "/admin/briefs/4",
    "/admin/briefs/4/convert",
    "/admin/briefs/4/convert?error=validation",
    "/admin/briefs/503",
)

# Non-200 HTML fixtures for Reviewer evidence (route → expected HTTP status).
# Register preview-only ids in app/admin_preview.py so ADMIN_PREVIEW_MODE
# renders populated error shells, not JSON or empty pages.
ADMIN_SCREENSHOT_EXPECTED_STATUS: dict[str, int] = {
    "/admin/briefs/503": 503,
}


def _active_nav_label(active_path: str) -> str:
    """Return the label for the current admin section."""
    for link in ADMIN_NAV_LINKS:
        if link["href"] == active_path:
            return link["label"]
    return "Admin"


def render_admin_nav(active_path: str) -> str:
    """Return the admin sidebar navigation list.

    Desktop gets a always-visible list outside ``<details>``. Mobile uses a
    collapsed ``details/summary`` disclosure for the same links. Keeping the
    desktop list *outside* ``details`` avoids fighting the UA rule that hides
    non-summary children of closed details.
    """
    current_label = html.escape(_active_nav_label(active_path))
    items: list[str] = []
    for link in ADMIN_NAV_LINKS:
        href = link["href"]
        label = html.escape(link["label"])
        attrs = [f'href="{href}"', 'class="admin-nav-link"']
        if active_path == href:
            attrs.append('aria-current="page"')
        items.append(f"          <li><a {' '.join(attrs)}>{label}</a></li>")
    items_html = "\n".join(items)
    return f"""        <nav class="admin-nav" aria-label="Admin">
          <ul class="admin-nav-list admin-nav-desktop">
{items_html}
          </ul>
          <details class="admin-nav-toggle">
            <summary
              class="admin-nav-summary"
              aria-label="Admin sections. Current: {current_label}. Expand for all sections."
            >
              <span class="admin-nav-current">{current_label}</span>
              <span class="admin-nav-expand-label">Menu</span>
            </summary>
            <ul class="admin-nav-list admin-nav-mobile-list">
{items_html}
            </ul>
          </details>
        </nav>"""


def render_admin_shell(
    *,
    title: str,
    main: str,
    active_path: str,
    admin_username: str = "",
    csrf_token: str = "",
) -> str:
    """Return a complete admin HTML document."""
    page_title = html.escape(title)
    nav = render_admin_nav(active_path)
    user_chip = ""
    if admin_username:
        user_chip = (
            f'<span class="admin-user">Signed in as '
            f"<strong>{html.escape(admin_username)}</strong></span>"
        )
    csrf_input = ""
    if csrf_token:
        csrf_input = (
            '<input type="hidden" name="csrf_token" '
            f'value="{html.escape(csrf_token, quote=True)}" />'
        )
    return f"""<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <meta name="robots" content="noindex, nofollow" />
    <title>{page_title} · saberistic admin</title>
    <link rel="icon" href="/assets/logo.png" type="image/png" />
    <link rel="preconnect" href="https://fonts.googleapis.com" />
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
    <link
      href="https://fonts.googleapis.com/css2?family=Archivo+Black&family=IBM+Plex+Mono:wght@400;500;600&display=swap"
      rel="stylesheet"
    />
    <link rel="stylesheet" href="/assets/site.css" />
    <link rel="stylesheet" href="/assets/admin.css" />
  </head>
  <body class="admin-app">
    <header class="admin-top">
      <a class="admin-brand" href="/admin" aria-label="saberistic admin home">
        <img
          class="brand-word"
          src="/assets/logo-text.png"
          width="160"
          height="41"
          alt="saberistic"
        />
        <span class="admin-badge">Admin</span>
      </a>
      <div class="admin-top-actions">
        {user_chip}
        <a class="admin-exit" href="/">Public site</a>
        <form method="post" action="/admin/logout">
          {csrf_input}
          <button class="admin-exit admin-signout" type="submit">Sign out</button>
        </form>
      </div>
    </header>
    <div class="admin-layout">
{nav}
      <main class="admin-main" id="main-content">
{main}
      </main>
    </div>
  </body>
</html>
"""
