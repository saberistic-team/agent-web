"""Shared layout fragments for the private admin interface."""

from __future__ import annotations

import html

ADMIN_NAV_LINKS: tuple[dict[str, str], ...] = (
    {
        "label": "Dashboard",
        "href": "/admin",
        "milestone": "Admin foundation",
        "summary": "Operational overview and brief summaries",
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
)


def render_admin_nav(active_path: str) -> str:
    """Return the admin sidebar navigation list."""
    items: list[str] = []
    for link in ADMIN_NAV_LINKS:
        href = link["href"]
        label = html.escape(link["label"])
        attrs = [f'href="{href}"', 'class="admin-nav-link"']
        if active_path == href:
            attrs.append('aria-current="page"')
        items.append(f'          <li><a {" ".join(attrs)}>{label}</a></li>')
    items_html = "\n".join(items)
    return f"""        <nav class="admin-nav" aria-label="Admin">
          <details class="admin-nav-toggle" open>
            <summary class="admin-nav-summary">Sections</summary>
            <ul class="admin-nav-list">
{items_html}
            </ul>
          </details>
        </nav>"""


def render_admin_shell(
    *,
    title: str,
    main: str,
    active_path: str,
    csrf_token: str | None = None,
) -> str:
    """Return a complete admin HTML document."""
    page_title = html.escape(title)
    nav = render_admin_nav(active_path)
    logout_html = ""
    if csrf_token:
        safe_csrf = html.escape(csrf_token, quote=True)
        logout_html = f"""      <form method="post" action="/admin/logout" class="admin-logout-form">
        <input type="hidden" name="csrf_token" value="{safe_csrf}" />
        <button class="admin-exit admin-logout" type="submit">Sign out</button>
      </form>"""
    else:
        logout_html = '      <a class="admin-exit" href="/">Public site</a>'
    return f"""<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <meta name="robots" content="noindex, nofollow" />
    <title>{page_title} · saberistic admin</title>
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
{logout_html}
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
