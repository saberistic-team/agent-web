"""HTML for admin authentication pages."""

from __future__ import annotations

from app.admin_layout import render_admin_shell

import html
from datetime import datetime
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo
import json

from app.brief_service import BriefListFilters
from app.config import Settings


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


def render_admin_dashboard_page(
    *,
    admin_username: str,
    settings: Settings,
    csrf_token: str,
) -> str:
    username = html.escape(admin_username)
    base_url = html.escape(settings.base_url, quote=True)
    return f"""<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Admin — saberistic</title>
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
      <form method="post" action="/admin/logout">
        <input type="hidden" name="csrf_token" value="{html.escape(csrf_token, quote=True)}" />
        <button class="top-link admin-logout" type="submit">Sign out</button>
      </form>
    </header>

    <main>
      <section class="block admin-page" aria-labelledby="admin-home-title">
        <h1 class="page-title" id="admin-home-title">Admin</h1>
        <p class="admin-lede">Signed in as <strong>{username}</strong>.</p>
        <p class="admin-note">
          Intake browse and management tools are intentionally deferred.
          This area is reserved for authenticated operator routes at
          <code>{base_url}/admin</code>.
        </p>
      </section>
    </main>

    <footer class="foot">
      <p>saberistic · technical architecture &amp; engineering leadership</p>
    </footer>
  </body>
</html>
"""

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


def _format_brief_status(status: str) -> str:
    labels = {
        "pending_payment": "Pending",
        "paid": "Paid",
        "abandoned": "Abandoned",
    }
    return labels.get(status, status)


def _format_amount(cents: int) -> str:
    return f"${cents / 100:.0f}"


def _format_utm(source: str | None, campaign: str | None) -> str:
    parts: list[str] = []
    if source:
        parts.append(source)
    if campaign:
        parts.append(campaign)
    if not parts:
        return '<span class="audit-muted">—</span>'
    return html.escape(" / ".join(parts))


def _briefs_query_params(
    filters: BriefListFilters,
    *,
    page: int | None = None,
) -> dict[str, str]:
    params: dict[str, str] = {}
    target_page = page if page is not None else filters.page
    if target_page > 1:
        params["page"] = str(target_page)
    if filters.query:
        params["q"] = filters.query
    if filters.status:
        params["status"] = filters.status
    if filters.date_from_raw:
        params["date_from"] = filters.date_from_raw
    if filters.date_to_raw:
        params["date_to"] = filters.date_to_raw
    return params


def _briefs_href(filters: BriefListFilters, *, page: int | None = None) -> str:
    params = _briefs_query_params(filters, page=page)
    if not params:
        return "/admin/briefs"
    return f"/admin/briefs?{urlencode(params)}"


def render_admin_briefs_page(
    *,
    admin_username: str,
    briefs: list[dict[str, Any]],
    filters: BriefListFilters,
    total: int,
    price_cents: int,
    csrf_token: str = "",
    db_error: bool = False,
) -> str:
    rows: list[str] = []
    for brief in briefs:
        brief_id = brief.get("id", "")
        status = str(brief.get("status", ""))
        status_label = _format_brief_status(status)
        status_class = html.escape(status, quote=True)
        payment_cell = f'<span class="admin-status admin-status-{status_class}">{html.escape(status_label)}</span>'
        paid_at = brief.get("paid_at")
        if status == "paid":
            paid_parts = [_format_amount(price_cents)]
            if paid_at:
                paid_parts.append(_format_timestamp(paid_at))
            payment_cell = "<br>".join(paid_parts)
        website = html.escape(str(brief.get("website", "")))
        email = html.escape(str(brief.get("contact_value", "")))
        back_params = _briefs_query_params(filters)
        detail_suffix = f"?{urlencode(back_params)}" if back_params else ""
        detail_href = (
            f"/admin/briefs/{html.escape(str(brief_id), quote=True)}{detail_suffix}"
        )
        rows.append(
            "<tr>"
            f'<td><a class="brief-row-link" href="{detail_href}">#{html.escape(str(brief_id))}</a></td>'
            f"<td>{_format_timestamp(brief.get('created_at', ''))}</td>"
            f"<td>{website}</td>"
            f"<td>{email}</td>"
            f"<td>{payment_cell}</td>"
            f"<td>{_format_utm(brief.get('utm_source'), brief.get('utm_campaign'))}</td>"
            "</tr>"
        )

    has_filters = bool(
        filters.query or filters.status or filters.date_from_raw or filters.date_to_raw
    )
    if db_error:
        table_body = (
            '<tr><td colspan="6" class="audit-empty">'
            "Briefs are temporarily unavailable. Try again shortly."
            "</td></tr>"
        )
    elif not rows:
        if has_filters:
            empty_message = "No briefs match your filters."
        else:
            empty_message = "No project briefs submitted yet."
        table_body = f'<tr><td colspan="6" class="audit-empty">{empty_message}</td></tr>'
    else:
        table_body = "\n".join(rows)

    total_pages = max((total + filters.per_page - 1) // filters.per_page, 1)
    prev_link = ""
    if filters.page > 1:
        prev_href = html.escape(_briefs_href(filters, page=filters.page - 1), quote=True)
        prev_link = f'<a class="audit-pager-link" href="{prev_href}">Previous</a>'
    next_link = ""
    if filters.page < total_pages:
        next_href = html.escape(_briefs_href(filters, page=filters.page + 1), quote=True)
        next_link = f'<a class="audit-pager-link" href="{next_href}">Next</a>'

    q_value = html.escape(filters.query or "", quote=True)
    date_from_value = html.escape(filters.date_from_raw or "", quote=True)
    date_to_value = html.escape(filters.date_to_raw or "", quote=True)
    status_options = [
        ("", "All statuses"),
        ("pending_payment", "Pending payment"),
        ("paid", "Paid"),
        ("abandoned", "Abandoned"),
    ]
    status_select = "\n".join(
        (
            f'<option value="{html.escape(value, quote=True)}"'
            f'{" selected" if filters.status == value else ""}>'
            f"{html.escape(label)}</option>"
        )
        for value, label in status_options
    )

    error_banner = ""
    if db_error:
        error_banner = (
            '<p class="brief-error" role="alert">'
            "Could not load briefs from the database."
            "</p>"
        )

    main = f"""        <section class="admin-panel" aria-labelledby="admin-briefs-title">
          <p class="admin-eyebrow">Brief intake</p>
          <h1 class="admin-title" id="admin-briefs-title">Submitted briefs</h1>
          <p class="admin-lede">
            Read-only list of project brief leads. Brief text and payment identifiers
            are not shown here.
          </p>
          {error_banner}
          <form class="brief-filters" method="get" action="/admin/briefs">
            <div class="brief-filters-row">
              <label class="brief-filter">
                <span class="brief-filter-label">Search</span>
                <input
                  type="search"
                  name="q"
                  value="{q_value}"
                  maxlength="100"
                  placeholder="ID, website, or email"
                  autocomplete="off"
                />
              </label>
              <label class="brief-filter">
                <span class="brief-filter-label">Status</span>
                <select name="status">
                  {status_select}
                </select>
              </label>
              <label class="brief-filter">
                <span class="brief-filter-label">From</span>
                <input type="date" name="date_from" value="{date_from_value}" />
              </label>
              <label class="brief-filter">
                <span class="brief-filter-label">To</span>
                <input type="date" name="date_to" value="{date_to_value}" />
              </label>
              <button class="brief-filter-submit" type="submit">Apply</button>
            </div>
          </form>
          <div class="audit-meta">
            <span>{total} briefs</span>
            <span>Page {filters.page} of {total_pages}</span>
          </div>
          <div class="audit-table-wrap">
            <table class="audit-table brief-table">
              <thead>
                <tr>
                  <th scope="col">ID</th>
                  <th scope="col">Submitted (UTC)</th>
                  <th scope="col">Website</th>
                  <th scope="col">Email</th>
                  <th scope="col">Payment</th>
                  <th scope="col">UTM</th>
                </tr>
              </thead>
              <tbody>
{table_body}
              </tbody>
            </table>
          </div>
          <nav class="audit-pager" aria-label="Briefs pagination">
            {prev_link}
            {next_link}
          </nav>
        </section>"""
    return render_admin_shell(
        title="Briefs",
        main=main,
        active_path="/admin/briefs",
        admin_username=admin_username,
        csrf_token=csrf_token,
    )


def _format_optional_text(value: Any) -> str:
    if value is None or str(value).strip() == "":
        return '<span class="audit-muted">—</span>'
    return html.escape(str(value))


def _format_stripe_reference(value: str | None) -> str:
    """Render a Stripe identifier in body text only — never in titles or metadata."""
    if not value or not str(value).strip():
        return '<span class="audit-muted">—</span>'
    return f'<code class="brief-stripe-ref">{html.escape(str(value))}</code>'


def _brief_stripe_references(brief: dict[str, Any]) -> tuple[str, str] | None:
    """Return Stripe reference rows when they help operators reconcile payment state."""
    status = str(brief.get("status", ""))
    session_id = brief.get("stripe_session_id")
    intent_id = brief.get("stripe_payment_intent_id")
    if status == "paid" and (session_id or intent_id):
        return (
            _format_stripe_reference(session_id),
            _format_stripe_reference(intent_id),
        )
    if status == "pending_payment" and session_id:
        return (
            _format_stripe_reference(session_id),
            '<span class="audit-muted">—</span>',
        )
    if status == "abandoned" and session_id:
        return (
            _format_stripe_reference(session_id),
            '<span class="audit-muted">—</span>',
        )
    return None


def render_admin_brief_db_error(
    *,
    admin_username: str,
    back_filters: BriefListFilters,
    retry_href: str,
    csrf_token: str = "",
) -> str:
    back_href = html.escape(_briefs_href(back_filters), quote=True)
    safe_retry_href = html.escape(retry_href, quote=True)
    main = f"""        <section class="admin-panel" aria-labelledby="admin-brief-db-error-title">
          <p class="brief-detail-back">
            <a class="audit-pager-link" href="{back_href}">← Back to briefs</a>
          </p>
          <p class="admin-eyebrow">Brief intake</p>
          <h1 class="admin-title" id="admin-brief-db-error-title">Brief temporarily unavailable</h1>
          <p class="brief-error" role="alert">
            Could not load briefs from the database.
          </p>
          <p class="admin-lede">
            Briefs are temporarily unavailable. Try again shortly.
          </p>
          <p class="admin-note">
            <a href="{safe_retry_href}">Retry loading this brief</a>
            or <a href="{back_href}">return to the briefs list</a>.
          </p>
        </section>"""
    return render_admin_shell(
        title="Brief unavailable",
        main=main,
        active_path="/admin/briefs",
        admin_username=admin_username,
        csrf_token=csrf_token,
    )


def render_admin_brief_not_found(
    *,
    brief_id: int,
    admin_username: str,
    back_filters: BriefListFilters,
    csrf_token: str = "",
) -> str:
    back_href = html.escape(_briefs_href(back_filters), quote=True)
    main = f"""        <section class="admin-panel" aria-labelledby="admin-brief-missing-title">
          <p class="brief-detail-back">
            <a class="audit-pager-link" href="{back_href}">← Back to briefs</a>
          </p>
          <p class="admin-eyebrow">Brief intake</p>
          <h1 class="admin-title" id="admin-brief-missing-title">Brief not found</h1>
          <p class="admin-lede">
            No project brief exists with ID #{html.escape(str(brief_id))}.
          </p>
          <p class="admin-note">
            <a href="{back_href}">Return to the briefs list</a>.
          </p>
        </section>"""
    return render_admin_shell(
        title="Brief not found",
        main=main,
        active_path="/admin/briefs",
        admin_username=admin_username,
        csrf_token=csrf_token,
    )


def render_admin_brief_detail_page(
    *,
    admin_username: str,
    brief: dict[str, Any],
    back_filters: BriefListFilters,
    price_cents: int,
    csrf_token: str = "",
) -> str:
    brief_id = brief.get("id", "")
    status = str(brief.get("status", ""))
    status_label = _format_brief_status(status)
    status_class = html.escape(status, quote=True)
    status_html = (
        f'<span class="admin-status admin-status-{status_class}">'
        f"{html.escape(status_label)}</span>"
    )
    payment_lines = [status_html]
    if status == "paid":
        payment_lines.append(html.escape(_format_amount(price_cents)))
        paid_at = brief.get("paid_at")
        if paid_at:
            payment_lines.append(_format_timestamp(paid_at))
    payment_html = "<br>".join(payment_lines)

    stripe_refs = _brief_stripe_references(brief)
    stripe_section = ""
    if stripe_refs is not None:
        session_cell, intent_cell = stripe_refs
        stripe_section = f"""
          <section class="brief-detail-section" aria-labelledby="brief-stripe-title">
            <h2 class="brief-detail-heading" id="brief-stripe-title">Stripe references</h2>
            <p class="admin-note">For operator reconciliation only. Not indexed or logged.</p>
            <dl class="brief-detail-dl">
              <div class="brief-detail-row">
                <dt>Checkout session</dt>
                <dd>{session_cell}</dd>
              </div>
              <div class="brief-detail-row">
                <dt>Payment intent</dt>
                <dd>{intent_cell}</dd>
              </div>
            </dl>
          </section>"""

    back_href = html.escape(_briefs_href(back_filters), quote=True)
    website = html.escape(str(brief.get("website", "")))
    email = html.escape(str(brief.get("contact_value", "")))
    brief_text = html.escape(str(brief.get("brief", "")))

    main = f"""        <section class="admin-panel" aria-labelledby="admin-brief-detail-title">
          <p class="brief-detail-back">
            <a class="audit-pager-link" href="{back_href}">← Back to briefs</a>
          </p>
          <p class="admin-eyebrow">Brief intake</p>
          <h1 class="admin-title" id="admin-brief-detail-title">Project brief #{html.escape(str(brief_id))}</h1>
          <p class="admin-lede">
            Read-only intake record. Payment state is derived from Stripe — not editable here.
          </p>
          <dl class="brief-detail-dl">
            <div class="brief-detail-row">
              <dt>Submitted (UTC)</dt>
              <dd>{_format_timestamp(brief.get("created_at", ""))}</dd>
            </div>
            <div class="brief-detail-row">
              <dt>Website</dt>
              <dd class="brief-detail-url">{website}</dd>
            </div>
            <div class="brief-detail-row">
              <dt>Email</dt>
              <dd>{email}</dd>
            </div>
            <div class="brief-detail-row">
              <dt>Payment</dt>
              <dd>{payment_html}</dd>
            </div>
          </dl>
          <section class="brief-detail-section" aria-labelledby="brief-text-title">
            <h2 class="brief-detail-heading" id="brief-text-title">Project brief</h2>
            <div class="brief-detail-text">{brief_text}</div>
          </section>
          {stripe_section}
          <section class="brief-detail-section" aria-labelledby="brief-utm-title">
            <h2 class="brief-detail-heading" id="brief-utm-title">Attribution</h2>
            <dl class="brief-detail-dl">
              <div class="brief-detail-row">
                <dt>Source</dt>
                <dd>{_format_optional_text(brief.get("utm_source"))}</dd>
              </div>
              <div class="brief-detail-row">
                <dt>Medium</dt>
                <dd>{_format_optional_text(brief.get("utm_medium"))}</dd>
              </div>
              <div class="brief-detail-row">
                <dt>Campaign</dt>
                <dd>{_format_optional_text(brief.get("utm_campaign"))}</dd>
              </div>
              <div class="brief-detail-row">
                <dt>Content</dt>
                <dd>{_format_optional_text(brief.get("utm_content"))}</dd>
              </div>
              <div class="brief-detail-row">
                <dt>Term</dt>
                <dd>{_format_optional_text(brief.get("utm_term"))}</dd>
              </div>
            </dl>
          </section>
        </section>"""
    return render_admin_shell(
        title=f"Brief #{brief_id}",
        main=main,
        active_path="/admin/briefs",
        admin_username=admin_username,
        csrf_token=csrf_token,
    )


def render_admin_audit_page(
    *,
    admin_username: str,
    events: list[dict[str, Any]],
    page: int,
    per_page: int,
    total: int,
    csrf_token: str = "",
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
        csrf_token=csrf_token,
    )
