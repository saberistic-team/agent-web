"""Admin HTML for contact management."""

from __future__ import annotations

import html
from typing import Any
from uuid import UUID

from app.admin_layout import render_admin_shell
from app.contacts import (
    BUYING_ROLE_LABELS,
    BUYING_ROLES,
    EMAIL_PERMISSION_LABELS,
    EMAIL_PERMISSIONS,
    RELATIONSHIP_STRENGTH_LABELS,
    RELATIONSHIP_STRENGTHS,
    ContactDuplicateMatch,
    contact_display_name,
    format_contact_timestamp,
    render_buying_role_badges,
)
from app.crm_service import ContactListFilters


def _duplicate_warning_html(duplicates: list[ContactDuplicateMatch]) -> str:
    if not duplicates:
        return ""
    items = []
    for match in duplicates:
        name = html.escape(contact_display_name(match.contact))
        reason = html.escape(match.reason)
        contact_id = html.escape(str(match.contact_id), quote=True)
        items.append(
            f'<li><a href="/admin/contacts/{contact_id}/edit">{name}</a> — {reason}</li>'
        )
    return f"""
          <div class="admin-warning" role="alert">
            <p><strong>Possible duplicates</strong> — review before saving.</p>
            <ul class="admin-list">{"".join(items)}</ul>
            <label class="admin-checkbox">
              <input type="checkbox" name="confirm_duplicates" value="true" />
              Save anyway
            </label>
          </div>"""


def _company_filter_options(
    companies: list[dict[str, Any]],
    *,
    selected_id: UUID | None = None,
) -> str:
    options = ['<option value="">All companies</option>']
    for company in companies:
        company_id = str(company["id"])
        selected = " selected" if selected_id and str(selected_id) == company_id else ""
        name = html.escape(str(company.get("name", "")))
        safe_id = html.escape(company_id, quote=True)
        options.append(f'<option value="{safe_id}"{selected}>{name}</option>')
    return "\n".join(options)


def _company_options(
    companies: list[dict[str, Any]],
    *,
    selected_id: UUID | None = None,
) -> str:
    options = ['<option value="">No company</option>']
    for company in companies:
        company_id = str(company["id"])
        selected = " selected" if selected_id and str(selected_id) == company_id else ""
        name = html.escape(str(company.get("name", "")))
        safe_id = html.escape(company_id, quote=True)
        options.append(f'<option value="{safe_id}"{selected}>{name}</option>')
    return "\n".join(options)


def _role_checkboxes(selected_roles: list[str]) -> str:
    boxes = []
    for role in sorted(BUYING_ROLES):
        label = BUYING_ROLE_LABELS[role]
        checked = " checked" if role in selected_roles else ""
        safe_role = html.escape(role, quote=True)
        boxes.append(
            f"""
            <label class="admin-checkbox">
              <input type="checkbox" name="buying_roles" value="{safe_role}"{checked} />
              {html.escape(label)}
            </label>"""
        )
    return "\n".join(boxes)


def _select_options(
    options: dict[str, str],
    *,
    selected: str | None = None,
    include_blank: bool = True,
    blank_label: str = "—",
) -> str:
    rendered = []
    if include_blank:
        rendered.append(f'<option value="">{html.escape(blank_label)}</option>')
    for value, label in options.items():
        is_selected = " selected" if selected == value else ""
        rendered.append(
            f'<option value="{html.escape(value, quote=True)}"{is_selected}>'
            f"{html.escape(label)}</option>"
        )
    return "\n".join(rendered)


def _contact_form_fields(
    *,
    csrf_token: str,
    companies: list[dict[str, Any]],
    contact: dict[str, Any] | None = None,
    buying_roles: list[str] | None = None,
    duplicates: list[ContactDuplicateMatch] | None = None,
    form_action: str,
    submit_label: str,
) -> str:
    contact = contact or {}
    roles = buying_roles if buying_roles is not None else list(contact.get("buying_roles") or [])
    company_id = contact.get("company_id")
    selected_company = UUID(str(company_id)) if company_id else None
    last_interaction = contact.get("last_interaction_at")
    last_interaction_value = ""
    if last_interaction is not None:
        if hasattr(last_interaction, "strftime"):
            last_interaction_value = last_interaction.strftime("%Y-%m-%dT%H:%M")
        else:
            last_interaction_value = str(last_interaction)
    return f"""
          <form class="admin-form contact-form" method="post" action="{html.escape(form_action, quote=True)}">
            <input type="hidden" name="csrf_token" value="{html.escape(csrf_token, quote=True)}" />
            {_duplicate_warning_html(duplicates or [])}
            <div class="field">
              <label for="full_name">Name</label>
              <input id="full_name" name="full_name" type="text" required maxlength="500"
                value="{html.escape(str(contact.get('full_name') or ''))}" />
            </div>
            <div class="field">
              <label for="title">Title</label>
              <input id="title" name="title" type="text" maxlength="500"
                value="{html.escape(str(contact.get('title') or ''))}" />
            </div>
            <div class="field">
              <label for="profile_url">LinkedIn / profile URL</label>
              <input id="profile_url" name="profile_url" type="url" maxlength="2000"
                placeholder="https://linkedin.com/in/..."
                value="{html.escape(str(contact.get('profile_url') or ''))}" />
            </div>
            <div class="field">
              <label for="email">Email (optional)</label>
              <input id="email" name="email" type="email" maxlength="500"
                value="{html.escape(str(contact.get('email') or ''))}" />
            </div>
            <div class="field">
              <label for="email_provenance">Email provenance</label>
              <input id="email_provenance" name="email_provenance" type="text" maxlength="1000"
                placeholder="How we obtained this address"
                value="{html.escape(str(contact.get('email_provenance') or ''))}" />
            </div>
            <div class="field">
              <label for="email_permission">Email permission</label>
              <select id="email_permission" name="email_permission">
                {_select_options(EMAIL_PERMISSION_LABELS, selected=str(contact.get('email_permission') or '') or None)}
              </select>
            </div>
            <div class="field">
              <label for="company_id">Company</label>
              <select id="company_id" name="company_id">
                {_company_options(companies, selected_id=selected_company)}
              </select>
            </div>
            <fieldset class="contact-role-fieldset">
              <legend>Buying roles</legend>
              {_role_checkboxes(roles)}
            </fieldset>
            <div class="field">
              <label for="relationship_strength">Relationship strength</label>
              <select id="relationship_strength" name="relationship_strength">
                {_select_options(RELATIONSHIP_STRENGTH_LABELS, selected=str(contact.get('relationship_strength') or '') or None)}
              </select>
            </div>
            <div class="field">
              <label for="last_interaction_at">Last interaction (ISO 8601)</label>
              <input id="last_interaction_at" name="last_interaction_at" type="text"
                placeholder="2026-07-14T12:00:00Z"
                value="{html.escape(last_interaction_value)}" />
            </div>
            <div class="field">
              <label for="notes">Notes</label>
              <textarea id="notes" name="notes" rows="4" maxlength="10000">{html.escape(str(contact.get('notes') or ''))}</textarea>
            </div>
            <button class="cta admin-submit" type="submit">{html.escape(submit_label)}</button>
          </form>"""


def render_admin_contacts_page(
    *,
    contacts: list[dict[str, Any]],
    total: int,
    filters: ContactListFilters,
    companies: list[dict[str, Any]],
    csrf_token: str,
    admin_username: str = "",
    error_message: str | None = None,
    notice_message: str | None = None,
) -> str:
    error_html = ""
    if error_message:
        error_html = f'<p class="form-error" role="alert">{html.escape(error_message)}</p>'
    notice_html = ""
    if notice_message:
        notice_html = f'<p class="admin-notice" role="status">{html.escape(notice_message)}</p>'
    company_filter_options = _company_filter_options(companies, selected_id=filters.company_id)
    rows = ""
    if contacts:
        for contact in contacts:
            contact_id = html.escape(str(contact["id"]), quote=True)
            name = html.escape(str(contact.get("display_name") or contact_display_name(contact)))
            title = html.escape(str(contact.get("title") or ""))
            company_name = html.escape(str(contact.get("company_name") or "—"))
            email = html.escape(str(contact.get("email") or "—"))
            roles = render_buying_role_badges(list(contact.get("buying_roles") or []))
            last_touch = format_contact_timestamp(contact.get("last_interaction_at")) or "—"
            strength = html.escape(
                RELATIONSHIP_STRENGTH_LABELS.get(
                    str(contact.get("relationship_strength") or ""),
                    str(contact.get("relationship_strength") or "—"),
                )
            )
            archived_badge = ""
            if contact.get("archived_at"):
                archived_badge = '<span class="contact-archived-badge">Archived</span> '
            rows += f"""
            <tr>
              <td>{archived_badge}<a href="/admin/contacts/{contact_id}/edit">{name}</a></td>
              <td>{title or "—"}</td>
              <td>{company_name}</td>
              <td>{email}</td>
              <td>{roles}</td>
              <td>{strength}</td>
              <td>{last_touch}</td>
            </tr>"""
    else:
        rows = """
            <tr>
              <td colspan="7">No contacts match this filter.</td>
            </tr>"""
    archived_checked = " checked" if filters.include_archived else ""
    query_value = html.escape(filters.query or "")
    main = f"""        <section class="admin-contacts" aria-labelledby="contacts-title">
          <p class="admin-eyebrow">CRM</p>
          <h1 class="admin-title" id="contacts-title">Contacts</h1>
          <p class="admin-lede">People, buying roles, and relationship context for acquisition.</p>
          {error_html}
          {notice_html}
          <p class="admin-actions">
            <a class="cta" href="/admin/contacts/new">New contact</a>
          </p>
          <form class="admin-filters" method="get" action="/admin/contacts">
            <div class="field">
              <label for="q">Search</label>
              <input id="q" name="q" type="search" value="{query_value}" placeholder="Name, email, title, company" />
            </div>
            <div class="field">
              <label for="company_id">Company</label>
              <select id="company_id" name="company_id">
                {company_filter_options}
              </select>
            </div>
            <label class="admin-checkbox">
              <input type="checkbox" name="archived" value="1"{archived_checked} />
              Include archived
            </label>
            <button class="cta admin-submit" type="submit">Filter</button>
          </form>
          <div class="admin-table-wrap">
            <table class="admin-table">
              <thead>
                <tr>
                  <th scope="col">Name</th>
                  <th scope="col">Title</th>
                  <th scope="col">Company</th>
                  <th scope="col">Email</th>
                  <th scope="col">Buying roles</th>
                  <th scope="col">Strength</th>
                  <th scope="col">Last interaction</th>
                </tr>
              </thead>
              <tbody>{rows}
              </tbody>
            </table>
          </div>
          <p class="admin-note">{total} contact(s)</p>
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
    duplicates: list[ContactDuplicateMatch] | None = None,
    error_message: str | None = None,
    is_edit: bool = False,
) -> str:
    title = "Edit contact" if is_edit else "New contact"
    contact_id = ""
    if contact and contact.get("id"):
        contact_id = html.escape(str(contact["id"]), quote=True)
    error_html = ""
    if error_message:
        error_html = f'<p class="form-error" role="alert">{html.escape(error_message)}</p>'
    breadcrumb = '<p class="admin-breadcrumb"><a href="/admin/contacts">Contacts</a></p>'
    if is_edit and contact_id:
        breadcrumb = (
            f'<p class="admin-breadcrumb">'
            f'<a href="/admin/contacts">Contacts</a> / '
            f'<a href="/admin/contacts/{contact_id}">'
            f'{html.escape(contact_display_name(contact or {}))}</a></p>'
        )
    form_action = (
        f"/admin/contacts/{contact_id}/edit" if is_edit and contact_id else "/admin/contacts"
    )
    submit_label = "Save changes" if is_edit else "Create contact"
    archive_form = ""
    if is_edit and contact and not contact.get("archived_at"):
        archive_form = f"""
          <form class="admin-inline-form" method="post" action="/admin/contacts/{contact_id}/archive">
            <input type="hidden" name="csrf_token" value="{html.escape(csrf_token, quote=True)}" />
            <button class="admin-danger" type="submit">Archive contact</button>
          </form>"""
    research_link = ""
    if is_edit and contact_id:
        research_link = (
            f'<p class="admin-actions">'
            f'<a href="/admin/contacts/{contact_id}">Research records</a></p>'
        )
    main = f"""        <section class="admin-contact-form" aria-labelledby="contact-form-title">
          {breadcrumb}
          <h1 class="admin-title" id="contact-form-title">{html.escape(title)}</h1>
          {error_html}
          {research_link}
          {_contact_form_fields(
              csrf_token=csrf_token,
              companies=companies,
              contact=contact,
              buying_roles=buying_roles,
              duplicates=duplicates,
              form_action=form_action,
              submit_label=submit_label,
          )}
          {archive_form}
        </section>"""
    return render_admin_shell(
        title=title,
        main=main,
        active_path="/admin/contacts",
        admin_username=admin_username,
        csrf_token=csrf_token,
    )
