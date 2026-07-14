"""Admin HTML for contact list, create, and edit pages."""

from __future__ import annotations

import html
from typing import Any
from uuid import UUID

from app.admin_layout import render_admin_shell
from app.contacts import BUYING_ROLE_LABELS, BUYING_ROLES, DuplicateWarning, format_buying_roles
from app.research_records import format_record_timestamp


def _render_duplicate_warnings(warnings: list[DuplicateWarning]) -> str:
    if not warnings:
        return ""
    items = ""
    for warning in warnings:
        contact_id = html.escape(warning.contact_id, quote=True)
        label = html.escape(warning.label)
        reason = html.escape(warning.reason.replace("_", " "))
        items += (
            f'<li>Possible duplicate ({reason}): '
            f'<a href="/admin/contacts/{contact_id}">{label}</a></li>'
        )
    return f"""
          <div class="admin-warning" role="status">
            <p class="admin-warning-title">Possible duplicates</p>
            <ul class="admin-list">{items}
            </ul>
          </div>"""


def _render_buying_role_checkboxes(
    *,
    selected: list[str],
    prefix: str = "",
) -> str:
    boxes = ""
    for role in sorted(BUYING_ROLES):
        checked = " checked" if role in selected else ""
        label = html.escape(BUYING_ROLE_LABELS[role])
        name = f"{prefix}buying_roles" if prefix else "buying_roles"
        boxes += f"""
            <label class="admin-checkbox">
              <input type="checkbox" name="{html.escape(name, quote=True)}" value="{html.escape(role, quote=True)}"{checked} />
              {label}
            </label>"""
    return f"""
          <fieldset class="admin-checkbox-group">
            <legend>Buying roles</legend>
            {boxes}
          </fieldset>"""


def _company_options(
    companies: list[dict[str, Any]],
    *,
    selected_id: UUID | None,
) -> str:
    options = '<option value="">No company</option>'
    for company in companies:
        company_id = str(company["id"])
        selected = " selected" if selected_id is not None and str(selected_id) == company_id else ""
        name = html.escape(str(company.get("name", "")))
        options += (
            f'<option value="{html.escape(company_id, quote=True)}"{selected}>{name}</option>'
        )
    return options


def _contact_form_fields(
    *,
    csrf_token: str,
    companies: list[dict[str, Any]],
    contact: dict[str, Any] | None = None,
    buying_roles: list[str] | None = None,
    action: str,
    submit_label: str,
) -> str:
    contact = contact or {}
    buying_roles = buying_roles or []
    company_id = contact.get("company_id")
    selected_company = UUID(str(company_id)) if company_id is not None else None
    full_name = html.escape(str(contact.get("full_name") or ""), quote=True)
    title = html.escape(str(contact.get("title") or ""), quote=True)
    email = html.escape(str(contact.get("email") or ""), quote=True)
    profile_url = html.escape(str(contact.get("profile_url") or ""), quote=True)
    email_provenance = html.escape(str(contact.get("email_provenance") or ""), quote=True)
    email_permission = html.escape(str(contact.get("email_permission") or ""), quote=True)
    notes = html.escape(str(contact.get("notes") or ""))
    relationship = contact.get("relationship_strength")
    relationship_value = (
        html.escape(str(relationship), quote=True) if relationship is not None else ""
    )
    last_interaction = contact.get("last_interaction_at")
    last_interaction_value = ""
    if last_interaction is not None:
        last_interaction_value = html.escape(
            last_interaction.isoformat().replace("+00:00", "Z"),
            quote=True,
        )
    company_options = _company_options(companies, selected_id=selected_company)
    role_boxes = _render_buying_role_checkboxes(selected=buying_roles)
    return f"""
          <input type="hidden" name="csrf_token" value="{html.escape(csrf_token, quote=True)}" />
          <div class="field">
            <label for="full_name">Name</label>
            <input id="full_name" name="full_name" type="text" required maxlength="500" value="{full_name}" />
          </div>
          <div class="field">
            <label for="title">Title</label>
            <input id="title" name="title" type="text" maxlength="500" value="{title}" />
          </div>
          <div class="field">
            <label for="company_id">Company</label>
            <select id="company_id" name="company_id">
              {company_options}
            </select>
          </div>
          <div class="field">
            <label for="profile_url">Profile URL</label>
            <input id="profile_url" name="profile_url" type="url" maxlength="2000" placeholder="https://linkedin.com/in/..." value="{profile_url}" />
          </div>
          <div class="field">
            <label for="email">Email (optional)</label>
            <input id="email" name="email" type="email" maxlength="500" value="{email}" />
          </div>
          <div class="field">
            <label for="email_provenance">Email provenance</label>
            <input id="email_provenance" name="email_provenance" type="text" maxlength="500" placeholder="e.g. brief intake, conference badge" value="{email_provenance}" />
          </div>
          <div class="field">
            <label for="email_permission">Email permission</label>
            <input id="email_permission" name="email_permission" type="text" maxlength="500" placeholder="e.g. explicit opt-in, inferred from public posting" value="{email_permission}" />
          </div>
          <div class="field">
            <label for="relationship_strength">Relationship strength (1–5)</label>
            <input id="relationship_strength" name="relationship_strength" type="number" min="1" max="5" value="{relationship_value}" />
          </div>
          <div class="field">
            <label for="last_interaction_at">Last interaction (ISO 8601)</label>
            <input id="last_interaction_at" name="last_interaction_at" type="text" placeholder="2026-07-14T12:00:00Z" value="{last_interaction_value}" />
          </div>
          <div class="field">
            <label for="notes">Notes</label>
            <textarea id="notes" name="notes" rows="4" maxlength="10000">{notes}</textarea>
          </div>
          {role_boxes}
          <button class="cta admin-submit" type="submit" formaction="{html.escape(action, quote=True)}">{html.escape(submit_label)}</button>"""


def render_admin_contacts_page(
    *,
    contacts: list[dict[str, Any]],
    total: int,
    query: str | None,
    include_archived: bool,
    csrf_token: str,
    admin_username: str = "",
) -> str:
    rows = ""
    if contacts:
        for contact in contacts:
            contact_id = html.escape(str(contact["id"]), quote=True)
            name = html.escape(str(contact.get("full_name") or "—"))
            title = html.escape(str(contact.get("title") or "—"))
            company = html.escape(str(contact.get("company_name") or "—"))
            email = html.escape(str(contact.get("email") or "—"))
            roles = html.escape(format_buying_roles(contact.get("buying_roles") or []) or "—")
            last_touch = format_record_timestamp(contact.get("last_interaction_at"))
            status = str(contact.get("status", "active"))
            status_html = ""
            if status == "archived":
                status_html = ' <span class="admin-status admin-status-archived">archived</span>'
            rows += f"""
            <tr>
              <td><a href="/admin/contacts/{contact_id}">{name}</a>{status_html}</td>
              <td>{title}</td>
              <td>{company}</td>
              <td>{email}</td>
              <td>{roles}</td>
              <td>{last_touch}</td>
            </tr>"""
    else:
        rows = """
            <tr>
              <td colspan="6">No contacts match this search.</td>
            </tr>"""
    query_value = html.escape(query or "", quote=True)
    archived_checked = " checked" if include_archived else ""
    main = f"""        <section class="admin-contacts" aria-labelledby="contacts-title">
          <p class="admin-eyebrow">CRM</p>
          <h1 class="admin-title" id="contacts-title">Contacts</h1>
          <p class="admin-lede">People, buying roles, and relationship context for acquisition.</p>
          <p><a class="cta admin-cta-inline" href="/admin/contacts/new">New contact</a></p>
          <form class="admin-form admin-filter-form" method="get" action="/admin/contacts">
            <div class="field">
              <label for="q">Search</label>
              <input id="q" name="q" type="search" value="{query_value}" placeholder="Name, email, title, company…" />
            </div>
            <label class="admin-checkbox">
              <input type="checkbox" name="include_archived" value="1"{archived_checked} />
              Include archived
            </label>
            <button class="cta admin-submit" type="submit">Search</button>
          </form>
          <p class="admin-note">{total} contact(s)</p>
          <div class="admin-table-wrap">
            <table class="admin-table">
              <thead>
                <tr>
                  <th scope="col">Name</th>
                  <th scope="col">Title</th>
                  <th scope="col">Company</th>
                  <th scope="col">Email</th>
                  <th scope="col">Buying roles</th>
                  <th scope="col">Last interaction</th>
                </tr>
              </thead>
              <tbody>{rows}
              </tbody>
            </table>
          </div>
        </section>"""
    return render_admin_shell(
        title="Contacts",
        main=main,
        active_path="/admin/contacts",
        admin_username=admin_username,
        csrf_token=csrf_token,
    )


def render_admin_contact_form_page(
    *,
    companies: list[dict[str, Any]],
    csrf_token: str,
    admin_username: str = "",
    contact: dict[str, Any] | None = None,
    buying_roles: list[str] | None = None,
    duplicate_warnings: list[DuplicateWarning] | None = None,
    error_message: str | None = None,
    is_edit: bool = False,
) -> str:
    contact = contact or {}
    contact_id = contact.get("id")
    title_text = "Edit contact" if is_edit else "New contact"
    action = (
        f"/admin/contacts/{html.escape(str(contact_id), quote=True)}/edit"
        if is_edit and contact_id
        else "/admin/contacts"
    )
    error_html = ""
    if error_message:
        error_html = (
            f'<p class="form-error" role="alert">{html.escape(error_message)}</p>'
        )
    warnings_html = _render_duplicate_warnings(duplicate_warnings or [])
    form_fields = _contact_form_fields(
        csrf_token=csrf_token,
        companies=companies,
        contact=contact,
        buying_roles=buying_roles,
        action=action,
        submit_label="Save contact",
    )
    archive_html = ""
    if is_edit and contact_id and str(contact.get("status", "active")) != "archived":
        archive_html = f"""
          <form class="admin-form admin-inline-form" method="post" action="/admin/contacts/{html.escape(str(contact_id), quote=True)}/archive">
            <input type="hidden" name="csrf_token" value="{html.escape(csrf_token, quote=True)}" />
            <button class="admin-exit admin-archive" type="submit">Archive contact</button>
          </form>"""
    main = f"""        <section class="admin-contacts" aria-labelledby="contact-form-title">
          <p class="admin-breadcrumb"><a href="/admin/contacts">Contacts</a> / {html.escape(title_text)}</p>
          <h1 class="admin-title" id="contact-form-title">{html.escape(title_text)}</h1>
          {error_html}
          {warnings_html}
          <form class="admin-form" method="post" action="{action}">
            {form_fields}
          </form>
          {archive_html}
        </section>"""
    return render_admin_shell(
        title=title_text,
        main=main,
        active_path="/admin/contacts",
        admin_username=admin_username,
        csrf_token=csrf_token,
    )
