"""Shared layout fragments for public site pages."""

from __future__ import annotations

import html

PRIMARY_NAV_LINKS: tuple[dict[str, str | bool], ...] = (
    {"label": "Services", "href": "/services"},
    {"label": "Case studies", "href": "/case-studies"},
    {"label": "Insights", "href": "/insights"},
    {"label": "About", "href": "/about"},
    {"label": "Diagnostic", "href": "/brief", "primary": True},
)

PAGE_PATH_BY_FILENAME: dict[str, str | None] = {
    "index.html": "/",
    "about.html": "/about",
    "services.html": "/services",
    "case-studies.html": "/case-studies",
    "brief.html": "/brief",
    "brief-success.html": "/brief/success",
    "404.html": None,
}


def render_site_header(active_path: str | None = None) -> str:
    """Return the shared site header with primary navigation."""
    nav_links: list[str] = []
    for link in PRIMARY_NAV_LINKS:
        href = str(link["href"])
        label = html.escape(str(link["label"]))
        classes = ["top-link"]
        if link.get("primary"):
            classes.append("top-link-primary")
        attrs = [f'class="{" ".join(classes)}"', f'href="{href}"']
        attrs.append(f'data-nav-destination="{href}"')
        if active_path == href:
            attrs.append('aria-current="page"')
        nav_links.append(f'        <a {" ".join(attrs)}>{label}</a>')

    nav_html = "\n".join(nav_links)
    return f"""    <header class="top">
      <a class="brand" href="/" aria-label="saberistic home">
        <img
          class="brand-word"
          src="/assets/logo-text.png"
          width="160"
          height="41"
          alt="saberistic"
        />
      </a>
      <nav class="top-nav" aria-label="Primary">
{nav_html}
      </nav>
    </header>"""
