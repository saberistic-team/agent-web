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


def render_admin_dashboard_page(
    *,
    admin_username: str = "",
    csrf_token: str = "",
) -> str:
    main = """        <section class="admin-empty" aria-labelledby="admin-dashboard-title">
          <p class="admin-eyebrow">Dashboard</p>
          <h1 class="admin-title" id="admin-dashboard-title">Operations</h1>
          <p class="admin-lede">Use Companies and Contacts to manage acquisition targets and relationships.</p>
          <p class="admin-note"><a href="/admin/contacts">View contacts</a> · <a href="/admin/companies">View companies</a></p>
        </section>"""
    return render_admin_shell(
        title="Dashboard",
        main=main,
        active_path="/admin",
        admin_username=admin_username,
        csrf_token=csrf_token,
    )


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
    if get_settings().admin_preview_enabled:
        from app.admin_preview import (
            build_preview_acquisition_dashboard_data,
            render_preview_section_main,
        )

        if active_path == "/admin":
            from app.admin_dashboard_pages import render_acquisition_dashboard_page

            main = render_acquisition_dashboard_page(
                data=build_preview_acquisition_dashboard_data(),
                admin_username=admin_username,
                csrf_token=csrf_token,
                preview_banner="Preview data — not production",
            )
            # render_acquisition_dashboard_page returns full shell; short-circuit.
            return main
        if active_path == "/admin/imports":
            from app.admin_preview import render_preview_imports_main

            main = render_preview_imports_main()
        elif active_path == "/admin/analytics":
            from app.admin_analytics_pages import render_analytics_dashboard_page
            from app.admin_preview import build_preview_analytics_dashboard_data

            return render_analytics_dashboard_page(
                data=build_preview_analytics_dashboard_data(),
                admin_username=admin_username,
                csrf_token=csrf_token,
                preview_banner="Preview data — not production",
            )
        else:
            main = render_preview_section_main(
                label=link["label"],
                summary=link["summary"],
                active_path=active_path,
            )
    elif active_path == "/admin":
        return render_admin_dashboard_page(
            admin_username=admin_username,
            csrf_token=csrf_token,
        )
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
