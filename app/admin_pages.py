"""HTML for admin authentication and audit pages."""

from __future__ import annotations

import html
import json
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.admin_layout import render_admin_shell


def _format_timestamp(value: datetime | str) -> str:
    if isinstance(value, str):
        return html.escape(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo("UTC"))
    return html.escape(value.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M:%S UTC"))


def _format_json_blob(data: Any) -> str:
    if data is None:
        return '<span class="audit-muted">—</span>'
    try:
        text = json.dumps(data, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        text = str(data)
    return f'<code class="audit-json">{html.escape(text)}</code>'


def render_admin_login_page(
    *,
    csrf_token: str,
    error_message: str | None = None,
    next_path: str | None = None,
) -> str:
    error_html = ""
    if error_message:
        error_html = (
            f'<p class="form-error" role="alert">{html.escape(error_message)}</p>'
        )
    next_field = ""
    if next_path:
        next_field = (
            f'<input type="hidden" name="next" value="{html.escape(next_path, quote=True)}" />'
        )
    return f"""<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Admin sign in — saberistic</title>
    <meta name="robots" content="noindex, nofollow" />
    <link rel="icon" href="/assets/logo.png" type="image/png" />
    <link rel="preconnect" href="https://fonts.googleapis.com" />
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
    <link
      href="https://fonts.googleapis.com/css2?family=Archivo+Black&family=IBM+Plex+Mono:wght@400;500&display=swap"
      rel="stylesheet"
    />
    <link rel="stylesheet" href="/assets/site.css" />
  </head>
  <body>
    <header class="top">
      <a class="brand" href="/" aria-label="saberistic home">
        <img
          class="brand-word"
          src="/assets/logo-text.png"
          width="160"
          height="41"
          alt="saberistic"
        />
      </a>
    </header>

    <main>
      <section class="block admin-page" aria-labelledby="admin-login-title">
        <h1 class="page-title" id="admin-login-title">Admin sign in</h1>
        <p class="admin-lede">Operator access only. No public registration.</p>
        <form class="admin-form" method="post" action="/admin/login">
          <input type="hidden" name="csrf_token" value="{html.escape(csrf_token, quote=True)}" />
          {next_field}
          <div class="field">
            <label for="username">Username</label>
            <input
              id="username"
              name="username"
              type="text"
              required
              autocomplete="username"
            />
          </div>
          <div class="field">
            <label for="password">Password</label>
            <input
              id="password"
              name="password"
              type="password"
              required
              autocomplete="current-password"
            />
          </div>
          {error_html}
          <button class="cta admin-submit" type="submit">Sign in</button>
        </form>
      </section>
    </main>

    <footer class="foot">
      <p>saberistic · technical architecture &amp; engineering leadership</p>
    </footer>
  </body>
</html>
"""


def render_admin_dashboard_page(*, admin_username: str) -> str:
    main = """        <section class="admin-panel" aria-labelledby="admin-home-title">
          <p class="admin-eyebrow">Dashboard</p>
          <h1 class="admin-title" id="admin-home-title">Operator console</h1>
          <p class="admin-lede">
            Acquisition tools ship in later milestones. Use the audit log to review
            security-sensitive admin activity.
          </p>
          <p class="admin-note"><a href="/admin/audit">View audit log</a></p>
        </section>"""
    return render_admin_shell(
        title="Dashboard",
        main=main,
        active_path="/admin",
        admin_username=admin_username,
    )


def render_admin_audit_page(
    *,
    admin_username: str,
    events: list[dict[str, Any]],
    page: int,
    per_page: int,
    total: int,
) -> str:
    rows: list[str] = []
    for event in events:
        entity = ""
        if event.get("entity_type"):
            entity_id = event.get("entity_id") or ""
            entity = html.escape(f"{event['entity_type']}:{entity_id}".rstrip(":"))
        entity_cell = entity or '<span class="audit-muted">—</span>'
        rows.append(
            "<tr>"
            f"<td>{_format_timestamp(event.get('created_at', ''))}</td>"
            f"<td>{html.escape(str(event.get('actor', '')))}</td>"
            f"<td><code>{html.escape(str(event.get('action', '')))}</code></td>"
            f"<td>{entity_cell}</td>"
            f"<td><code>{html.escape(str(event.get('correlation_id', '')))}</code></td>"
            f"<td>{_format_json_blob(event.get('summary_before'))}</td>"
            f"<td>{_format_json_blob(event.get('summary_after'))}</td>"
            "</tr>"
        )

    if not rows:
        table_body = (
            '<tr><td colspan="7" class="audit-empty">No audit events recorded yet.</td></tr>'
        )
    else:
        table_body = "\n".join(rows)

    total_pages = max((total + per_page - 1) // per_page, 1)
    prev_link = ""
    if page > 1:
        prev_link = f'<a class="audit-pager-link" href="/admin/audit?page={page - 1}">Previous</a>'
    next_link = ""
    if page < total_pages:
        next_link = f'<a class="audit-pager-link" href="/admin/audit?page={page + 1}">Next</a>'

    main = f"""        <section class="admin-panel" aria-labelledby="admin-audit-title">
          <p class="admin-eyebrow">Audit trail</p>
          <h1 class="admin-title" id="admin-audit-title">Immutable audit log</h1>
          <p class="admin-lede">
            Append-only record of security-sensitive admin mutations. Secrets and raw
            message bodies are never stored.
          </p>
          <div class="audit-meta">
            <span>{total} events</span>
            <span>Page {page} of {total_pages}</span>
          </div>
          <div class="audit-table-wrap">
            <table class="audit-table">
              <thead>
                <tr>
                  <th scope="col">Time (UTC)</th>
                  <th scope="col">Actor</th>
                  <th scope="col">Action</th>
                  <th scope="col">Entity</th>
                  <th scope="col">Correlation</th>
                  <th scope="col">Before</th>
                  <th scope="col">After</th>
                </tr>
              </thead>
              <tbody>
{table_body}
              </tbody>
            </table>
          </div>
          <nav class="audit-pager" aria-label="Audit pagination">
            {prev_link}
            {next_link}
          </nav>
        </section>"""
    return render_admin_shell(
        title="Audit log",
        main=main,
        active_path="/admin/audit",
        admin_username=admin_username,
    )


def render_admin_placeholder_page(*, admin_username: str, active_path: str, label: str) -> str:
    safe_label = html.escape(label)
    main = f"""        <section class="admin-empty" aria-labelledby="admin-empty-title">
          <p class="admin-eyebrow">Placeholder</p>
          <h1 class="admin-title" id="admin-empty-title">{safe_label}</h1>
          <p class="admin-lede">This section ships in a later milestone.</p>
          <p class="admin-note">Navigation shell is live; functionality arrives in a future issue.</p>
        </section>"""
    return render_admin_shell(
        title=label,
        main=main,
        active_path=active_path,
        admin_username=admin_username,
    )
