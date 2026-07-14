"""Admin HTML for company/contact research records."""

from __future__ import annotations

import html
from typing import Any

from app.admin_layout import render_admin_shell
from app.contacts import format_buying_roles
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


def _render_company_contact_row(contact: dict[str, Any]) -> str:
    contact_id = html.escape(str(contact["id"]), quote=True)
    name = html.escape(str(contact.get("full_name") or contact.get("email") or "Contact"))
    title = html.escape(str(contact.get("title") or ""))
    email = html.escape(str(contact.get("email") or ""))
    roles = format_buying_roles(contact.get("buying_roles") or [])
    roles_html = html.escape(roles) if roles else "—"
    strength = contact.get("relationship_strength")
    strength_html = html.escape(str(strength)) if strength is not None else "—"
    last_touch = format_record_timestamp(contact.get("last_interaction_at"))
    meta_parts = []
    if title:
        meta_parts.append(title)
    if email:
        meta_parts.append(email)
    meta = " · ".join(meta_parts)
    meta_html = f' <span class="admin-contact-meta">{html.escape(meta)}</span>' if meta else ""
    return f"""
            <tr>
              <td><a href="/admin/contacts/{contact_id}">{name}</a>{meta_html}</td>
              <td>{roles_html}</td>
              <td>{strength_html}</td>
              <td>{last_touch}</td>
            </tr>"""


def _render_contact_profile(contact: dict[str, Any], company: dict[str, Any] | None) -> str:
    name = html.escape(str(contact.get("full_name") or ""))
    title = html.escape(str(contact.get("title") or "—"))
    email = html.escape(str(contact.get("email") or "—"))
    profile_url = contact.get("profile_url")
    profile_html = "—"
    if profile_url:
        safe_url = html.escape(str(profile_url), quote=True)
        label = html.escape(str(profile_url))
        profile_html = f'<a href="{safe_url}" rel="noopener noreferrer">{label}</a>'
    provenance = html.escape(str(contact.get("email_provenance") or "—"))
    permission = html.escape(str(contact.get("email_permission") or "—"))
    strength = contact.get("relationship_strength")
    strength_html = html.escape(str(strength)) if strength is not None else "—"
    last_touch = format_record_timestamp(contact.get("last_interaction_at"))
    notes = html.escape(str(contact.get("notes") or ""))
    notes_html = f'<p class="admin-note">{notes}</p>' if notes else '<p class="admin-note">—</p>'
    roles = format_buying_roles(contact.get("buying_roles") or [])
    roles_html = html.escape(roles) if roles else "—"
    company_html = "—"
    if company is not None:
        company_id = html.escape(str(company["id"]), quote=True)
        company_name = html.escape(str(company.get("name", "")))
        company_html = f'<a href="/admin/companies/{company_id}">{company_name}</a>'
    status = str(contact.get("status", "active"))
    status_html = ""
    if status == "archived":
        status_html = ' <span class="admin-status admin-status-archived">Archived</span>'
    contact_id = html.escape(str(contact["id"]), quote=True)
    edit_link = f'<p><a class="cta admin-cta-inline" href="/admin/contacts/{contact_id}/edit">Edit contact</a></p>'
    return f"""
          <h2 class="admin-section-heading">Profile{status_html}</h2>
          {edit_link}
          <dl class="admin-detail-list">
            <div><dt>Name</dt><dd>{name or "—"}</dd></div>
            <div><dt>Title</dt><dd>{title}</dd></div>
            <div><dt>Company</dt><dd>{company_html}</dd></div>
            <div><dt>Profile URL</dt><dd>{profile_html}</dd></div>
            <div><dt>Email</dt><dd>{email}</dd></div>
            <div><dt>Email provenance</dt><dd>{provenance}</dd></div>
            <div><dt>Email permission</dt><dd>{permission}</dd></div>
            <div><dt>Buying roles</dt><dd>{roles_html}</dd></div>
            <div><dt>Relationship strength</dt><dd>{strength_html}</dd></div>
            <div><dt>Last interaction</dt><dd>{last_touch}</dd></div>
            <div><dt>Notes</dt><dd>{notes_html}</dd></div>
          </dl>"""


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
    error_html = ""
    if error_message:
        error_html = (
            f'<p class="form-error" role="alert">{html.escape(error_message)}</p>'
        )
    contact_rows = ""
    for contact in contacts:
        contact_rows += _render_company_contact_row(contact)
    if not contact_rows:
        contact_rows = """
            <tr>
              <td colspan="4">No contacts linked.</td>
            </tr>"""
    contact_table = f"""
          <div class="admin-table-wrap">
            <table class="admin-table">
              <thead>
                <tr>
                  <th scope="col">Contact</th>
                  <th scope="col">Buying roles</th>
                  <th scope="col">Strength</th>
                  <th scope="col">Last interaction</th>
                </tr>
              </thead>
              <tbody>{contact_rows}
              </tbody>
            </table>
          </div>
          <p><a class="cta admin-cta-inline" href="/admin/contacts/new">Add contact</a></p>"""
    contact_options = "\n".join(
        f'              <option value="{html.escape(str(contact["id"]), quote=True)}">'
        f'{html.escape(str(contact.get("email", "")))}</option>'
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
          <h2 class="admin-section-heading">Contacts</h2>
          {contact_table}
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
        str(contact.get("full_name") or contact.get("email") or "Contact")
    )
    contact_id = html.escape(str(contact["id"]), quote=True)
    profile_html = _render_contact_profile(contact, company)
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
    main = f"""        <section class="admin-research" aria-labelledby="contact-research-title">
          <p class="admin-breadcrumb"><a href="/admin/contacts">Contacts</a> / {display_name}</p>
          <h1 class="admin-title" id="contact-research-title">{display_name}</h1>
          <p class="admin-lede">Contact <code>{contact_id}</code>.</p>
          {profile_html}
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
        title=f"Contact — {display_name}",
        main=main,
        active_path="/admin/contacts",
        admin_username=admin_username,
        csrf_token=csrf_token,
    )
