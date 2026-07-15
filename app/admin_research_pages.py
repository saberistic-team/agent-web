"""Admin HTML for company/contact research records."""

from __future__ import annotations

import html
from typing import Any

from app.admin_layout import archive_action_button, render_admin_shell
from app.companies import COMPANY_CATEGORIES, COMPANY_STAGES, TARGET_STATUSES
from app.contacts import EMAIL_PERMISSIONS, RELATIONSHIP_STRENGTHS, format_buying_roles
from app.research_records import (
    RECORD_TYPE_LABELS,
    RESEARCH_RECORD_TYPES,
    format_record_timestamp,
    is_public_evidence_type,
    is_stale,
    record_ui_category,
    safe_source_link,
)


def _render_research_record_card(record: dict[str, Any]) -> str:
    record_type = str(record.get("record_type", ""))
    category = record_ui_category(record_type)
    label = RECORD_TYPE_LABELS.get(record_type, record_type)
    stale = is_stale(record)
    stale_html = (
        '<span class="research-stale-badge" role="status">Stale</span>'
        if stale
        else ""
    )
    body = html.escape(str(record.get("body", "")))
    provenance_html = ""
    if is_public_evidence_type(record_type):
        source_name = str(record.get("source_name") or "")
        source_url = record.get("source_url")
        observed_value = html.escape(str(record.get("observed_value") or ""))
        confidence = record.get("confidence")
        confidence_text = (
            html.escape(f"{float(confidence):.0%}")
            if confidence is not None
            else ""
        )
        source_link = ""
        if source_url:
            source_link = safe_source_link(
                str(source_url),
                label=source_name or str(source_url),
            )
        elif source_name:
            source_link = html.escape(source_name)
        provenance_html = f"""
          <dl class="research-provenance">
            <div><dt>Source</dt><dd>{source_link}</dd></div>
            <div><dt>Observed value</dt><dd>{observed_value}</dd></div>
            <div><dt>Observed at</dt><dd>{format_record_timestamp(record.get("observed_at"))}</dd></div>
            <div><dt>Confidence</dt><dd>{confidence_text}</dd></div>
            <div><dt>Review by</dt><dd>{format_record_timestamp(record.get("review_at"))}</dd></div>
            <div><dt>Expires</dt><dd>{format_record_timestamp(record.get("expires_at"))}</dd></div>
          </dl>"""
    return f"""
        <article class="research-record research-record--{html.escape(category, quote=True)}{' research-record--stale' if stale else ''}">
          <header class="research-record-header">
            <span class="research-type-badge research-type-badge--{html.escape(category, quote=True)}">{html.escape(label)}</span>
            {stale_html}
          </header>
          <p class="research-body">{body}</p>
          {provenance_html}
        </article>"""


def _research_form_body(*, csrf_token: str, contact_options: str = "") -> str:
    type_options = "\n".join(
        f'            <option value="{html.escape(record_type, quote=True)}">'
        f"{html.escape(RECORD_TYPE_LABELS[record_type])}</option>"
        for record_type in sorted(RESEARCH_RECORD_TYPES)
    )
    contact_field = ""
    if contact_options:
        contact_field = f"""
          <div class="field">
            <label for="contact_id">Contact (optional)</label>
            <select id="contact_id" name="contact_id">
              <option value="">Company-wide</option>
              {contact_options}
            </select>
          </div>"""
    return f"""
          <input type="hidden" name="csrf_token" value="{html.escape(csrf_token, quote=True)}" />
          <div class="field">
            <label for="record_type">Record type</label>
            <select id="record_type" name="record_type" required>
              {type_options}
            </select>
          </div>
          {contact_field}
          <div class="field">
            <label for="body">Summary</label>
            <textarea id="body" name="body" rows="3" required maxlength="10000"></textarea>
          </div>
          <fieldset class="research-evidence-fields">
            <legend>Public evidence (required for facts and signals)</legend>
            <div class="field">
              <label for="source_name">Source name</label>
              <input id="source_name" name="source_name" type="text" maxlength="500" />
            </div>
            <div class="field">
              <label for="source_url">Source URL</label>
              <input id="source_url" name="source_url" type="url" maxlength="2000" placeholder="https://..." />
            </div>
            <div class="field">
              <label for="observed_value">Observed value</label>
              <input id="observed_value" name="observed_value" type="text" maxlength="10000" />
            </div>
            <div class="field">
              <label for="observed_at">Observed at (ISO 8601)</label>
              <input id="observed_at" name="observed_at" type="text" placeholder="2026-07-14T12:00:00Z" />
            </div>
            <div class="field">
              <label for="confidence">Confidence (0–1)</label>
              <input id="confidence" name="confidence" type="number" min="0" max="1" step="0.01" />
            </div>
            <div class="field">
              <label for="review_at">Review by (ISO 8601)</label>
              <input id="review_at" name="review_at" type="text" placeholder="2026-08-14T12:00:00Z" />
            </div>
            <div class="field">
              <label for="expires_at">Expires (ISO 8601)</label>
              <input id="expires_at" name="expires_at" type="text" placeholder="2026-09-14T12:00:00Z" />
            </div>
          </fieldset>
          <button class="cta admin-submit" type="submit">Attach record</button>"""


def render_admin_companies_page(
    *,
    companies: list[dict[str, Any]],
    csrf_token: str,
    admin_username: str = "",
) -> str:
    rows = ""
    if companies:
        for company in companies:
            company_id = html.escape(str(company["id"]), quote=True)
            name = html.escape(str(company.get("name", "")))
            status = html.escape(str(company.get("status", "")))
            rows += f"""
            <tr>
              <td><a href="/admin/companies/{company_id}">{name}</a></td>
              <td>{status}</td>
            </tr>"""
    else:
        rows = """
            <tr>
              <td colspan="2">No companies yet.</td>
            </tr>"""
    main = f"""        <section class="admin-research" aria-labelledby="companies-title">
          <p class="admin-eyebrow">Research</p>
          <h1 class="admin-title" id="companies-title">Companies</h1>
          <p class="admin-lede">Attach research records to companies and contacts.</p>
          <div class="admin-table-wrap">
            <table class="admin-table">
              <thead>
                <tr>
                  <th scope="col">Name</th>
                  <th scope="col">Status</th>
                </tr>
              </thead>
              <tbody>{rows}
              </tbody>
            </table>
          </div>
        </section>"""
    return render_admin_shell(
        title="Companies",
        main=main,
        active_path="/admin/companies",
        admin_username=admin_username,
        csrf_token=csrf_token,
    )


def render_admin_company_research_page(
    *,
    company: dict[str, Any],
    contacts: list[dict[str, Any]],
    records: list[dict[str, Any]],
    csrf_token: str,
    admin_username: str = "",
    error_message: str | None = None,
) -> str:
    company_name = html.escape(str(company.get("name", "")))
    company_id = html.escape(str(company["id"]), quote=True)
    company_fields = (
        ("Domain", company.get("domain") or company.get("website")),
        ("Category", COMPANY_CATEGORIES.get(str(company.get("category")), company.get("category"))),
        ("Stage", COMPANY_STAGES.get(str(company.get("stage")), company.get("stage"))),
        ("Headcount estimate", company.get("headcount_estimate")),
        ("Funding", company.get("funding_summary")),
        ("Target status", TARGET_STATUSES.get(str(company.get("target_status")), company.get("target_status"))),
        ("Last verified", company.get("last_verified_at")),
    )
    facts_html = "".join(
        f"<div><dt>{html.escape(label)}</dt><dd>{html.escape(str(value or '—'))}</dd></div>"
        for label, value in company_fields
    )
    archive_action = "restore" if company.get("archived_at") else "archive"
    archive_label = "Restore company" if company.get("archived_at") else "Archive company"
    error_html = ""
    if error_message:
        error_html = (
            f'<p class="form-error" role="alert">{html.escape(error_message)}</p>'
        )
    contact_links = ""
    for contact in contacts:
        contact_id = html.escape(str(contact["id"]), quote=True)
        label = html.escape(
            str(contact.get("full_name") or contact.get("email") or contact.get("profile_url") or contact["id"])
        )
        title = html.escape(str(contact.get("title") or ""))
        roles = html.escape(format_buying_roles(contact.get("buying_roles")))
        meta = " · ".join(part for part in (title, roles) if part and part != "—")
        contact_links += (
            f'<li><a href="/admin/contacts/{contact_id}">{label}</a>'
            f'{f" <span class=\"admin-meta\">({meta})</span>" if meta else ""}</li>'
        )
    if not contact_links:
        contact_links = "<li>No contacts linked.</li>"
    contact_options = "\n".join(
        f'              <option value="{html.escape(str(contact["id"]), quote=True)}">'
        f'{html.escape(str(contact.get("full_name") or contact.get("email") or contact["id"]))}</option>'
        for contact in contacts
    )
    records_html = ""
    if records:
        records_html = "".join(_render_research_record_card(record) for record in records)
    else:
        records_html = '<p class="admin-note">No research records yet.</p>'
    form_body = _research_form_body(
        csrf_token=csrf_token,
        contact_options=contact_options,
    )
    main = f"""        <section class="admin-research" aria-labelledby="company-research-title">
          <p class="admin-breadcrumb"><a href="/admin/companies">Companies</a> / {company_name}</p>
          <h1 class="admin-title" id="company-research-title">{company_name}</h1>
          <p class="admin-lede">Research records for company <code>{company_id}</code>.</p>
          <p><a class="cta" href="/admin/companies/{company_id}/edit">Edit company</a></p>
          <dl class="research-provenance">{facts_html}</dl>
          <form method="post" action="/admin/companies/{company_id}/{archive_action}">
            <input type="hidden" name="csrf_token" value="{html.escape(csrf_token, quote=True)}" />
            {archive_action_button(label=archive_label, archived=bool(company.get("archived_at")))}
          </form>
          <h2 class="admin-section-heading">Contacts</h2>
          <p><a class="cta" href="/admin/contacts/new">Add contact</a></p>
          <ul class="admin-list">{contact_links}
          </ul>
          <h2 class="admin-section-heading">Attach record</h2>
          {error_html}
          <form class="admin-form research-form" method="post" action="/admin/companies/{company_id}/research">
            {form_body}
          </form>
          <h2 class="admin-section-heading">Records</h2>
          <div class="research-record-list">
            {records_html}
          </div>
        </section>"""
    return render_admin_shell(
        title=f"Research — {company_name}",
        main=main,
        active_path="/admin/companies",
        admin_username=admin_username,
        csrf_token=csrf_token,
    )


def render_admin_contact_research_page(
    *,
    contact: dict[str, Any],
    company: dict[str, Any] | None,
    records: list[dict[str, Any]],
    csrf_token: str,
    admin_username: str = "",
    error_message: str | None = None,
) -> str:
    display_name = html.escape(
        str(contact.get("full_name") or contact.get("email") or contact.get("profile_url") or contact["id"])
    )
    contact_id = html.escape(str(contact["id"]), quote=True)
    contact_fields = (
        ("Title", contact.get("title")),
        ("Profile", contact.get("profile_url")),
        ("Email", contact.get("email")),
        (
            "Email permission",
            EMAIL_PERMISSIONS.get(str(contact.get("email_permission")), contact.get("email_permission")),
        ),
        (
            "Buying roles",
            format_buying_roles(contact.get("buying_roles")),
        ),
        (
            "Relationship",
            RELATIONSHIP_STRENGTHS.get(
                str(contact.get("relationship_strength")), contact.get("relationship_strength")
            ),
        ),
        ("Last interaction", contact.get("last_interaction_at")),
        ("Notes", contact.get("notes")),
    )
    facts_html = "".join(
        f"<div><dt>{html.escape(label)}</dt><dd>{html.escape(str(value or '—'))}</dd></div>"
        for label, value in contact_fields
    )
    company_link = ""
    if company is not None:
        company_id = html.escape(str(company["id"]), quote=True)
        company_name = html.escape(str(company.get("name", "")))
        company_link = (
            f'<p class="admin-lede">Company: '
            f'<a href="/admin/companies/{company_id}">{company_name}</a></p>'
        )
    error_html = ""
    if error_message:
        error_html = (
            f'<p class="form-error" role="alert">{html.escape(error_message)}</p>'
        )
    records_html = ""
    if records:
        records_html = "".join(_render_research_record_card(record) for record in records)
    else:
        records_html = '<p class="admin-note">No research records yet.</p>'
    form_body = _research_form_body(csrf_token=csrf_token)
    archive_action = "restore" if contact.get("archived_at") else "archive"
    archive_label = "Restore contact" if contact.get("archived_at") else "Archive contact"
    main = f"""        <section class="admin-research" aria-labelledby="contact-research-title">
          <p class="admin-breadcrumb"><a href="/admin/contacts">Contacts</a></p>
          <h1 class="admin-title" id="contact-research-title">{display_name}</h1>
          {company_link}
          <p class="admin-lede">Research records for contact <code>{contact_id}</code>.</p>
          <p><a class="cta" href="/admin/contacts/{contact_id}/edit">Edit contact</a></p>
          <dl class="research-provenance">{facts_html}</dl>
          <form method="post" action="/admin/contacts/{contact_id}/{archive_action}">
            <input type="hidden" name="csrf_token" value="{html.escape(csrf_token, quote=True)}" />
            {archive_action_button(label=archive_label, archived=bool(contact.get("archived_at")))}
          </form>
          <h2 class="admin-section-heading">Attach record</h2>
          {error_html}
          <form class="admin-form research-form" method="post" action="/admin/contacts/{contact_id}/research">
            {form_body}
          </form>
          <h2 class="admin-section-heading">Records</h2>
          <div class="research-record-list">
            {records_html}
          </div>
        </section>"""
    return render_admin_shell(
        title=f"Research — {display_name}",
        main=main,
        active_path="/admin/contacts",
        admin_username=admin_username,
        csrf_token=csrf_token,
    )
