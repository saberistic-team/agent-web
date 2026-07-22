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
        "label": "Targets",
        "href": "/admin/targets",
        "milestone": "Qualification workspace",
        "summary": "Tier A/B/C lists with evidence gaps and freshness",
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
    "/admin/briefs/5/convert",
    "/admin/briefs/6/convert",
    "/admin/briefs/7/convert",
    "/admin/briefs/503",
    "/admin/companies/dddddddd-dddd-dddd-dddd-dddddddddd01",
    "/admin/companies/dddddddd-dddd-dddd-dddd-dddddddddd02",
    "/admin/contacts/dddddddd-dddd-dddd-dddd-dddddddddd03",
    "/admin/contacts/dddddddd-dddd-dddd-dddd-dddddddddd04",
    "/admin/contacts/dddddddd-dddd-dddd-dddd-dddddddddd03/edit",
    "/admin/contacts/dddddddd-dddd-dddd-dddd-dddddddddd04/edit",
    "/admin/companies?archived=1",
    "/admin/contacts?archived=1",
    "/admin/contacts/eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee/restore-conflict",
    # Company detail/editor fixtures (see docs/SCREENSHOTS.md).
    "/admin/companies/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    "/admin/companies/new",
    "/admin/companies/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/edit",
    "/admin/companies/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa02",
    "/admin/companies/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/edit?error=validation&focus=name",
    # Contact detail/editor fixtures.
    "/admin/contacts/bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    "/admin/contacts/new",
    "/admin/contacts/bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb/edit",
    "/admin/contacts/bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbc/edit",
    # Pipeline detail (Next action, Change stage, Log activity, timeline).
    "/admin/pipeline/11111111-1111-1111-1111-111111111111",
    "/admin/pipeline/11111111-1111-1111-1111-111111111111?error=validation&focus=expected_value_cents",
    "/admin/targets/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa01",
    # ICP scoring list, rules editor, and company score detail fixtures.
    "/admin/signals/rules",
    "/admin/signals/11111111-1111-1111-1111-111111111111",
    "/admin/signals/22222222-2222-2222-2222-222222222222",
    "/admin/discovery/runs/bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb1",
)

# Non-200 HTML fixtures for Reviewer evidence (route → expected HTTP status).
# Register preview-only ids in app/admin_preview.py so ADMIN_PREVIEW_MODE
# renders populated error shells, not JSON or empty pages.
ADMIN_SCREENSHOT_EXPECTED_STATUS: dict[str, int] = {
    "/admin/briefs/503": 503,
}


def render_admin_archive_action_button(*, label: str, archived: bool) -> str:
    """Return a themed Archive/Restore submit button for admin detail/edit forms."""
    modifier = "admin-action--secondary" if archived else "admin-action--destructive"
    return (
        f'<button class="admin-action {modifier}" type="submit">'
        f"{html.escape(label)}</button>"
    )


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
        # title= exposes the untruncated value to hover/AT when the wrap
        # strategy below still leaves the identity visually tight.
        safe_username_attr = html.escape(admin_username, quote=True)
        safe_username = html.escape(admin_username)
        user_chip = (
            f'<span class="admin-user">Signed in as '
            f'<strong title="{safe_username_attr}">{safe_username}</strong></span>'
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
        <div class="admin-exit-group">
          <a class="admin-exit" href="/">Public site</a>
          <form class="admin-signout-form" method="post" action="/admin/logout">
            {csrf_input}
            <button class="admin-exit admin-signout" type="submit">Sign out</button>
          </form>
        </div>
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
