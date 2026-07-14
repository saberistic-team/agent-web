"""Admin dashboard pages and placeholder empty states."""

from __future__ import annotations

import html

from app.admin_layout import ADMIN_NAV_LINKS, ADMIN_PATHS, render_admin_shell
from app.config import get_settings

_LINK_BY_HREF = {link["href"]: link for link in ADMIN_NAV_LINKS}


def _render_empty_state(link: dict[str, str]) -> str:
    label = html.escape(link["label"])
    milestone = html.escape(link["milestone"])
    summary = html.escape(link["summary"])
    return f"""        <section class="admin-empty" aria-labelledby="admin-empty-title">
          <p class="admin-eyebrow">Placeholder</p>
          <h1 class="admin-title" id="admin-empty-title">{label}</h1>
          <p class="admin-lede">{summary} will ship in the <strong>{milestone}</strong> milestone.</p>
          <p class="admin-note">This navigation shell is live; functionality arrives in a later issue.</p>
        </section>"""


def render_admin_page(
    active_path: str,
    *,
    admin_username: str = "",
    csrf_token: str = "",
) -> str:
    """Render an admin section page within the shared shell."""
    link = _LINK_BY_HREF.get(active_path)
    if link is None:
        return render_admin_not_found(
            active_path,
            admin_username=admin_username,
            csrf_token=csrf_token,
        )
    if active_path == "/admin" and get_settings().admin_preview_enabled:
        from app.admin_preview import (
            build_preview_dashboard_data,
            render_preview_dashboard_main,
        )

        main = render_preview_dashboard_main(build_preview_dashboard_data())
    else:
        main = _render_empty_state(link)
    return render_admin_shell(
        title=link["label"],
        main=main,
        active_path=active_path,
        admin_username=admin_username,
        csrf_token=csrf_token,
    )


def render_admin_not_found(
    path: str,
    *,
    admin_username: str = "",
    csrf_token: str = "",
) -> str:
    """Render an admin 404 within the shared shell."""
    safe_path = html.escape(path)
    main = f"""        <section class="admin-empty" aria-labelledby="admin-not-found-title">
          <p class="admin-eyebrow">Not found</p>
          <h1 class="admin-title" id="admin-not-found-title">Unknown admin page</h1>
          <p class="admin-lede">No section exists at <code>{safe_path}</code>.</p>
          <p class="admin-note"><a href="/admin">Return to the dashboard</a>.</p>
        </section>"""
    return render_admin_shell(
        title="Not found",
        main=main,
        active_path="",
        admin_username=admin_username,
        csrf_token=csrf_token,
    )


def is_admin_path(path: str) -> bool:
    """Return True if path is a known admin section."""
    return path in ADMIN_PATHS
