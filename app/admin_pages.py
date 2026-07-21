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
from app.pipeline_stages import pipeline_stage_label
from app import audit_service


AUDIT_ACTION_LABELS: dict[str, str] = {
    audit_service.ACTION_AUTH_LOGIN_SUCCESS: "Login success",
    audit_service.ACTION_AUTH_LOGIN_FAILURE: "Login failure",
    audit_service.ACTION_AUTH_LOGOUT: "Logout",
    audit_service.ACTION_IMPORT_BATCH: "Import batch",
    audit_service.ACTION_IMPORT_BATCH_ROLLBACK: "Import rollback",
    audit_service.ACTION_ENTITY_DELETE: "Entity delete",
    audit_service.ACTION_COMPANY_CREATE: "Company create",
    audit_service.ACTION_COMPANY_UPDATE: "Company update",
    audit_service.ACTION_COMPANY_ARCHIVE: "Company archive",
    audit_service.ACTION_COMPANY_RESTORE: "Company restore",
    audit_service.ACTION_CONTACT_CREATE: "Contact create",
    audit_service.ACTION_CONTACT_UPDATE: "Contact update",
    audit_service.ACTION_CONTACT_ARCHIVE: "Contact archive",
    audit_service.ACTION_PIPELINE_UPDATE: "Pipeline update",
    audit_service.ACTION_SCORING_RULE_UPDATE: "Scoring rule update",
    audit_service.ACTION_ANALYTICS_CONFIG_UPDATE: "Analytics config update",
    audit_service.ACTION_EXPORT_REQUEST: "Export request",
    audit_service.ACTION_BRIEF_CONVERT: "Brief convert",
    audit_service.ACTION_CONTACT_RESTORE: "Contact restore",
    audit_service.ACTION_RESEARCH_RECORD_CREATE: "Research evidence added",
    audit_service.ACTION_PIPELINE_ACTIVITY_CREATE: "Pipeline activity logged",
}


def _format_audit_action(action: str) -> str:
    label = AUDIT_ACTION_LABELS.get(action, action.replace(".", " ").title())
    return (
        f'<span class="audit-action-label">{html.escape(label)}</span> '
        f'<code class="audit-action-code">{html.escape(action)}</code>'
    )


def _format_bounded_audit_summary(action: str, summary: Any) -> str:
    """Render escaped, bounded summaries for known audit event types."""
    if summary is None:
        return '<span class="audit-muted">—</span>'
    if not isinstance(summary, dict):
        return _format_json_blob(summary)
    if action == audit_service.ACTION_RESEARCH_RECORD_CREATE:
        parts = [
            f"type={html.escape(str(summary.get('record_type', '')))}",
            f"company={html.escape(str(summary.get('company_id', '')))}",
        ]
        if summary.get("contact_id"):
            parts.append(f"contact={html.escape(str(summary['contact_id']))}")
        flags = [
            name.replace("has_", "")
            for name, present in summary.items()
            if name.startswith("has_") and present
        ]
        if flags:
            parts.append(f"fields={html.escape(', '.join(flags))}")
        return f'<code class="audit-json">{", ".join(parts)}</code>'
    if action == audit_service.ACTION_PIPELINE_ACTIVITY_CREATE:
        parts = [
            f"type={html.escape(str(summary.get('activity_type', '')))}",
            f"company={html.escape(str(summary.get('company_id', '')))}",
        ]
        if summary.get("contact_id"):
            parts.append(f"contact={html.escape(str(summary['contact_id']))}")
        if summary.get("created_at"):
            parts.append(f"at={html.escape(str(summary['created_at']))}")
        return f'<code class="audit-json">{", ".join(parts)}</code>'
    if action in {
        audit_service.ACTION_COMPANY_CREATE,
        audit_service.ACTION_CONTACT_CREATE,
    }:
        label_key = "name" if action == audit_service.ACTION_COMPANY_CREATE else "full_name"
        parts = [f"{label_key}={html.escape(str(summary.get(label_key, '')))}"]
        for field in ("domain", "category", "stage", "target_status", "company_id"):
            if summary.get(field):
                parts.append(f"{field}={html.escape(str(summary[field]))}")
        return f'<code class="audit-json">{", ".join(parts)}</code>'
    if action in {
        audit_service.ACTION_COMPANY_ARCHIVE,
        audit_service.ACTION_COMPANY_RESTORE,
        audit_service.ACTION_CONTACT_ARCHIVE,
        audit_service.ACTION_CONTACT_RESTORE,
    }:
        label_key = (
            "name"
            if action.startswith("company.")
            else "full_name"
        )
        parts = []
        if summary.get(label_key):
            parts.append(f"{label_key}={html.escape(str(summary[label_key]))}")
        if "archived_at" in summary:
            archived = summary.get("archived_at")
            parts.append(
                f"archived_at={html.escape(str(archived) if archived is not None else 'cleared')}"
            )
        return f'<code class="audit-json">{", ".join(parts) if parts else "—"}</code>'
    return _format_json_blob(summary)


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
        <form class="admin-form admin-form--compact" method="post" action="/admin/login">
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


def _format_currency_upper(currency: str | None) -> str:
    return (currency or "usd").upper()


def _brief_paid_amount_cents(brief: dict[str, Any], *, list_price_cents: int) -> int:
    paid_amount = brief.get("payment_amount_cents")
    if paid_amount is not None:
        return int(paid_amount)
    return list_price_cents


def _brief_payment_summary_lines(
    brief: dict[str, Any],
    *,
    list_price_cents: int,
    detailed: bool = False,
) -> list[str]:
    """Render paid brief amounts; legacy rows without payment columns use list price."""
    subtotal = brief.get("payment_subtotal_cents")
    discount = brief.get("payment_discount_cents")
    amount = brief.get("payment_amount_cents")
    currency = _format_currency_upper(brief.get("payment_currency"))

    if amount is None and subtotal is None:
        return [_format_amount(list_price_cents)]

    final_cents = int(amount) if amount is not None else list_price_cents
    if not detailed:
        if discount and subtotal is not None:
            return [
                f"{_format_amount(int(subtotal))} − {_format_amount(int(discount))} "
                f"= {_format_amount(final_cents)} {currency}"
            ]
        return [_format_amount(final_cents)]

    lines: list[str] = []
    if subtotal is not None:
        lines.append(f"Subtotal: {_format_amount(int(subtotal))} {currency}")
    if discount:
        lines.append(f"Discount: −{_format_amount(int(discount))} {currency}")
    lines.append(f"Total: {_format_amount(final_cents)} {currency}")
    return lines


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
            paid_parts = _brief_payment_summary_lines(
                brief,
                list_price_cents=price_cents,
            )
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


def _brief_stripe_references(brief: dict[str, Any]) -> tuple[str, str, str, str] | None:
    """Return Stripe reference rows when they help operators reconcile payment state."""
    status = str(brief.get("status", ""))
    session_id = brief.get("stripe_session_id")
    intent_id = brief.get("stripe_payment_intent_id")
    promotion_code_id = brief.get("stripe_promotion_code_id")
    coupon_id = brief.get("stripe_coupon_id")
    if status == "paid" and (session_id or intent_id or promotion_code_id or coupon_id):
        return (
            _format_stripe_reference(session_id),
            _format_stripe_reference(intent_id),
            _format_stripe_reference(promotion_code_id),
            _format_stripe_reference(coupon_id),
        )
    if status == "pending_payment" and session_id:
        return (
            _format_stripe_reference(session_id),
            '<span class="audit-muted">—</span>',
            '<span class="audit-muted">—</span>',
            '<span class="audit-muted">—</span>',
        )
    if status == "abandoned" and session_id:
        return (
            _format_stripe_reference(session_id),
            '<span class="audit-muted">—</span>',
            '<span class="audit-muted">—</span>',
            '<span class="audit-muted">—</span>',
        )
    return None


def render_admin_brief_database_unavailable(
    *,
    admin_username: str,
    back_filters: BriefListFilters,
    retry_href: str,
    correlation_id: str,
    csrf_token: str = "",
) -> str:
    back_href = html.escape(_briefs_href(back_filters), quote=True)
    safe_retry_href = html.escape(retry_href, quote=True)
    safe_correlation = html.escape(correlation_id)
    main = f"""        <section class="admin-panel" aria-labelledby="admin-brief-db-error-title">
          <p class="brief-detail-back">
            <a class="audit-pager-link" href="{back_href}">← Back to briefs</a>
          </p>
          <p class="admin-eyebrow">Brief intake</p>
          <h1 class="admin-title" id="admin-brief-db-error-title">Briefs temporarily unavailable</h1>
          <p class="brief-error" role="alert">
            Could not load this brief from the database.
          </p>
          <p class="admin-lede">
            The brief database is temporarily unavailable. Try again shortly.
          </p>
          <p class="admin-note">
            <a class="cta admin-submit" href="{safe_retry_href}">Retry</a>
            <span class="audit-muted"> · </span>
            <a href="{back_href}">Return to the briefs list</a>
          </p>
          <p class="admin-note">
            Reference: <code>{safe_correlation}</code>
          </p>
        </section>"""
    return render_admin_shell(
        title="Briefs unavailable",
        main=main,
        active_path="/admin/briefs",
        admin_username=admin_username,
        csrf_token=csrf_token,
    )


def render_admin_brief_not_found(
    *,
    brief_id: int | str,
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
    pipeline_available: bool = False,
    conversion: dict[str, Any] | None = None,
    converted: bool = False,
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
        payment_lines.extend(
            html.escape(line)
            for line in _brief_payment_summary_lines(
                brief,
                list_price_cents=price_cents,
                detailed=True,
            )
        )
        paid_at = brief.get("paid_at")
        if paid_at:
            payment_lines.append(_format_timestamp(paid_at))
    payment_html = "<br>".join(payment_lines)

    stripe_refs = _brief_stripe_references(brief)
    stripe_section = ""
    if stripe_refs is not None:
        session_cell, intent_cell, promotion_cell, coupon_cell = stripe_refs
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
              <div class="brief-detail-row">
                <dt>Promotion code</dt>
                <dd>{promotion_cell}</dd>
              </div>
              <div class="brief-detail-row">
                <dt>Coupon</dt>
                <dd>{coupon_cell}</dd>
              </div>
            </dl>
          </section>"""

    back_href = html.escape(_briefs_href(back_filters), quote=True)
    website = html.escape(str(brief.get("website", "")))
    email = html.escape(str(brief.get("contact_value", "")))
    brief_text = html.escape(str(brief.get("brief", "")))

    pipeline_section = ""
    if conversion is not None:
        company = conversion.get("company") or {}
        contact = conversion.get("contact") or {}
        stage = conversion.get("pipeline_stage") or company.get("pipeline_stage")
        stage_label = html.escape(pipeline_stage_label(str(stage)) if stage else "—")
        company_href = html.escape(f"/admin/companies/{company.get('id')}", quote=True)
        contact_id = contact.get("id")
        contact_href = (
            html.escape(f"/admin/contacts/{contact_id}", quote=True) if contact_id else ""
        )
        contact_email = html.escape(str(contact.get("email", "—")))
        contact_cell = (
            f'<a href="{contact_href}">{contact_email}</a>' if contact_href else contact_email
        )
        pipeline_href = html.escape("/admin/pipeline", quote=True)
        pipeline_section = f"""
          <section class="brief-detail-section" aria-labelledby="brief-pipeline-linked-title">
            <h2 class="brief-detail-heading" id="brief-pipeline-linked-title">Pipeline linkage</h2>
            <p class="admin-note">This brief is linked to CRM and pipeline records.</p>
            <dl class="brief-detail-dl">
              <div class="brief-detail-row">
                <dt>Company</dt>
                <dd><a href="{company_href}">{html.escape(str(company.get("name", "—")))}</a></dd>
              </div>
              <div class="brief-detail-row">
                <dt>Contact</dt>
                <dd>{contact_cell}</dd>
              </div>
              <div class="brief-detail-row">
                <dt>Pipeline stage</dt>
                <dd><a href="{pipeline_href}">{stage_label}</a></dd>
              </div>
            </dl>
          </section>"""
    elif pipeline_available:
        convert_href = html.escape(
            f"/admin/briefs/{html.escape(str(brief_id), quote=True)}/convert",
            quote=True,
        )
        pipeline_section = f"""
          <section class="brief-detail-section" aria-labelledby="brief-pipeline-action-title">
            <h2 class="brief-detail-heading" id="brief-pipeline-action-title">Pipeline</h2>
            <p class="admin-note">
              Convert this intake record into company, contact, and pipeline records.
              The original brief text and payment state stay unchanged.
            </p>
            <p><a class="cta admin-submit" href="{convert_href}">Add to pipeline</a></p>
          </section>"""

    success_banner = ""
    if converted:
        success_banner = """
          <p class="admin-note" role="status">
            Brief added to pipeline. Linked records are shown below.
          </p>"""

    main = f"""        <section class="admin-panel" aria-labelledby="admin-brief-detail-title">
          <p class="brief-detail-back">
            <a class="audit-pager-link" href="{back_href}">← Back to briefs</a>
          </p>
          <p class="admin-eyebrow">Brief intake</p>
          <h1 class="admin-title" id="admin-brief-detail-title">Project brief #{html.escape(str(brief_id))}</h1>
          <p class="admin-lede">
            Read-only intake record. Payment state is derived from Stripe — not editable here.
          </p>
          {success_banner}
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
          {pipeline_section}
        </section>"""
    return render_admin_shell(
        title=f"Brief #{brief_id}",
        main=main,
        active_path="/admin/briefs",
        admin_username=admin_username,
        csrf_token=csrf_token,
    )


def _format_proposed_value(value: Any) -> str:
    if value is None or str(value).strip() == "":
        return '<span class="audit-muted">—</span>'
    return html.escape(str(value))


def _render_archived_contact_panel(*, archived: dict[str, Any]) -> str:
    contact_id = str(archived.get("id", ""))
    edit_href = html.escape(f"/admin/contacts/{contact_id}/edit", quote=True)
    full_name = html.escape(str(archived.get("full_name") or "—"))
    email = html.escape(str(archived.get("email") or "—"))
    company_name = archived.get("company_name")
    company_row = ""
    if company_name:
        company_row = (
            f'<div class="brief-detail-row">'
            f"<dt>Company</dt>"
            f"<dd>{html.escape(str(company_name))}</dd>"
            f"</div>"
        )
    archived_at = archived.get("archived_at")
    archived_display = (
        _format_timestamp(archived_at)
        if archived_at
        else '<span class="audit-muted">—</span>'
    )
    return f"""              <section class="brief-convert-archived" aria-labelledby="brief-convert-archived-title">
                <h3 class="brief-convert-archived-title" id="brief-convert-archived-title">Archived contact match</h3>
                <p class="brief-convert-archived-note">
                  This email matches an archived contact. Archived records are never linked
                  or restored automatically during conversion — review or restore separately.
                </p>
                <dl class="brief-detail-dl brief-convert-archived-dl">
                  <div class="brief-detail-row">
                    <dt>Name</dt>
                    <dd>{full_name}</dd>
                  </div>
                  <div class="brief-detail-row">
                    <dt>Email</dt>
                    <dd>{email}</dd>
                  </div>
                  {company_row}
                  <div class="brief-detail-row">
                    <dt>Archived</dt>
                    <dd>{archived_display}</dd>
                  </div>
                </dl>
                <p>
                  <a class="audit-pager-link" href="{edit_href}">Review or restore archived contact</a>
                </p>
              </section>"""


def _render_match_radios(
    *,
    choice_name: str,
    matches: list[dict[str, Any]],
) -> str:
    if not matches:
        return ""
    rows: list[str] = []
    for match in matches:
        match_id = str(match.get("id", ""))
        safe_id = html.escape(match_id, quote=True)
        label_parts = [str(match.get("name") or match.get("email") or match_id)]
        if match.get("domain"):
            label_parts.append(f"({match.get('domain')})")
        elif match.get("company_id"):
            label_parts.append(f"(company {match.get('company_id')})")
        label = html.escape(" ".join(label_parts))
        rows.append(
            f'<label class="brief-convert-match">'
            f'<input type="radio" name="{html.escape(choice_name, quote=True)}" '
            f'value="existing:{safe_id}" />'
            f" Link {label}</label>"
        )
    return "\n".join(rows)


def render_admin_brief_convert_page(
    *,
    admin_username: str,
    brief: dict[str, Any],
    back_filters: BriefListFilters,
    preview: dict[str, Any],
    csrf_token: str,
    error_message: str | None = None,
) -> str:
    brief_id = brief.get("id", "")
    back_href = html.escape(_briefs_href(back_filters), quote=True)
    detail_href = html.escape(f"/admin/briefs/{brief_id}", quote=True)
    proposal = preview.get("proposal") or {}
    company_matches: list[dict[str, Any]] = list(preview.get("company_matches") or [])
    contact_matches: list[dict[str, Any]] = list(preview.get("contact_matches") or [])
    archived_contact_match = preview.get("archived_contact_match")

    error_html = ""
    if error_message:
        error_html = (
            f'<p class="form-error" role="alert">{html.escape(error_message)}</p>'
        )

    company_match_html = _render_match_radios(
        choice_name="company_choice",
        matches=company_matches,
    )
    contact_match_html = _render_match_radios(
        choice_name="contact_choice",
        matches=contact_matches,
    )
    archived_panel_html = ""
    archived_ack_html = ""
    if archived_contact_match and not contact_matches:
        archived_panel_html = _render_archived_contact_panel(
            archived=archived_contact_match,
        )
        archived_ack_html = """
              <label class="admin-checkbox brief-convert-archived-ack">
                <input type="checkbox" name="acknowledge_archived_identity" value="1" />
                Create a new active contact — the archived identity will remain separate
              </label>"""

    default_company_choice = "existing" if company_matches else "new"
    if contact_matches:
        default_contact_choice = "existing"
    elif archived_contact_match:
        default_contact_choice = ""
    else:
        default_contact_choice = "new"
    company_new_checked = " checked" if default_company_choice == "new" else ""
    contact_new_checked = " checked" if default_contact_choice == "new" else ""

    expected_value = proposal.get("expected_value")
    expected_display = (
        f"${expected_value:.0f}" if isinstance(expected_value, (int, float)) else "—"
    )

    main = f"""        <section class="admin-panel" aria-labelledby="admin-brief-convert-title">
          <p class="brief-detail-back">
            <a class="audit-pager-link" href="{detail_href}">← Back to brief #{html.escape(str(brief_id))}</a>
          </p>
          <p class="admin-eyebrow">Brief intake</p>
          <h1 class="admin-title" id="admin-brief-convert-title">Add brief #{html.escape(str(brief_id))} to pipeline</h1>
          <p class="admin-lede">
            Review proposed CRM and pipeline fields. Existing matches are shown for your
            selection — nothing is merged automatically.
          </p>
          {error_html}
          <section class="brief-detail-section" aria-labelledby="brief-convert-preview-title">
            <h2 class="brief-detail-heading" id="brief-convert-preview-title">Proposed records</h2>
            <dl class="brief-detail-dl">
              <div class="brief-detail-row">
                <dt>Company name</dt>
                <dd>{_format_proposed_value(proposal.get("company_name"))}</dd>
              </div>
              <div class="brief-detail-row">
                <dt>Website / domain</dt>
                <dd>{_format_proposed_value(proposal.get("website"))} · {_format_proposed_value(proposal.get("domain"))}</dd>
              </div>
              <div class="brief-detail-row">
                <dt>Contact email</dt>
                <dd>{_format_proposed_value(proposal.get("contact_email"))}</dd>
              </div>
              <div class="brief-detail-row">
                <dt>Initial pipeline stage</dt>
                <dd>{html.escape(str(proposal.get("pipeline_stage_label", "—")))}</dd>
              </div>
              <div class="brief-detail-row">
                <dt>Expected value</dt>
                <dd>{html.escape(expected_display)}</dd>
              </div>
              <div class="brief-detail-row">
                <dt>Payment status (source)</dt>
                <dd>{_format_proposed_value(proposal.get("brief_status"))}</dd>
              </div>
            </dl>
          </section>
          <form class="admin-form admin-form--editor brief-convert-form" method="post" action="/admin/briefs/{html.escape(str(brief_id), quote=True)}/convert">
            <input type="hidden" name="csrf_token" value="{html.escape(csrf_token, quote=True)}" />
            <fieldset class="brief-convert-fieldset">
              <legend>Company</legend>
              <label class="brief-convert-choice">
                <input type="radio" name="company_choice" value="new"{company_new_checked} /> Create new company
              </label>
              {company_match_html}
            </fieldset>
            <fieldset class="brief-convert-fieldset">
              <legend>Contact</legend>
              <label class="brief-convert-choice">
                <input type="radio" name="contact_choice" value="new"{contact_new_checked} /> Create new contact
              </label>
              {contact_match_html}
              {archived_panel_html}
              {archived_ack_html}
            </fieldset>
            <button class="cta admin-submit" type="submit">Confirm and add to pipeline</button>
            <p class="admin-note"><a href="{detail_href}">Cancel</a></p>
          </form>
        </section>"""
    return render_admin_shell(
        title=f"Convert brief #{brief_id}",
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
    db_error: bool = False,
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
            f"<td>{_format_audit_action(str(event.get('action', '')))}</td>"
            f"<td>{entity_cell}</td>"
            f"<td><code>{html.escape(str(event.get('correlation_id', '')))}</code></td>"
            f"<td>{_format_bounded_audit_summary(str(event.get('action', '')), event.get('summary_before'))}</td>"
            f"<td>{_format_bounded_audit_summary(str(event.get('action', '')), event.get('summary_after'))}</td>"
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

    error_banner = ""
    if db_error:
        error_banner = (
            '          <p class="admin-error" role="alert">'
            "Audit log temporarily unavailable. Try again shortly."
            "</p>\n"
        )

    main = f"""        <section class="admin-panel" aria-labelledby="admin-audit-title">
          <p class="admin-eyebrow">Audit trail</p>
          <h1 class="admin-title" id="admin-audit-title">Immutable audit log</h1>
          <p class="admin-lede">
            Append-only record of security-sensitive admin mutations. Secrets and raw
            message bodies are never stored.
          </p>
{error_banner}          <div class="audit-meta">
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
