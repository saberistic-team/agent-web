"""Admin HTML for contact record management."""

from __future__ import annotations

import html
from typing import Any

from app.admin_layout import admin_archive_action_classes, render_admin_shell
from app.contacts import (
    BUYING_ROLES,
    EMAIL_PERMISSIONS,
    RELATIONSHIP_STRENGTHS,
    ContactSafeSummary,
    format_buying_roles,
)


def _esc(value: Any) -> str:
    return "" if value is None else html.escape(str(value), quote=True)


def _options(registry: dict[str, str], selected: str | None, *, empty: str = "Any") -> str:
    rows = [f'<option value="">{html.escape(empty)}</option>']
    rows.extend(
        f'<option value="{_esc(key)}"{" selected" if key == selected else ""}>{_esc(label)}</option>'
        for key, label in registry.items()
    )
    return "\n".join(rows)


def _company_options(companies: list[dict[str, Any]], selected: str | None) -> str:
    rows = ['<option value="">Unlinked</option>']
    rows.extend(
        f'<option value="{_esc(company["id"])}"{" selected" if str(company["id"]) == str(selected) else ""}>'
        f'{_esc(company.get("name"))}</option>'
        for company in companies
    )
    return "\n".join(rows)


def _role_checkboxes(selected: list[str] | None) -> str:
    selected_set = set(selected or [])
    return "\n".join(
        f'<label class="admin-checkbox"><input type="checkbox" name="buying_roles" value="{_esc(key)}"'
        f'{" checked" if key in selected_set else ""} /> {_esc(label)}</label>'
        for key, label in BUYING_ROLES.items()
    )


def _contact_form(
    *,
    action: str,
    csrf_token: str,
    companies: list[dict[str, Any]],
    contact: dict[str, Any] | None = None,
) -> str:
    contact = contact or {}
    roles = contact.get("buying_roles") or []
    return f"""<form class="admin-form" method="post" action="{_esc(action)}">
      <input type="hidden" name="csrf_token" value="{_esc(csrf_token)}" />
      <div class="field"><label for="full_name">Name</label><input id="full_name" name="full_name" required maxlength="500" value="{_esc(contact.get("full_name"))}" /></div>
      <div class="field"><label for="title">Title</label><input id="title" name="title" maxlength="500" value="{_esc(contact.get("title"))}" /></div>
      <div class="field"><label for="profile_url">LinkedIn / profile URL</label><input id="profile_url" name="profile_url" type="url" maxlength="2000" placeholder="https://linkedin.com/in/..." value="{_esc(contact.get("profile_url"))}" /></div>
      <div class="field"><label for="email">Email</label><input id="email" name="email" type="email" maxlength="320" value="{_esc(contact.get("email"))}" /></div>
      <div class="field"><label for="email_permission">Email permission</label><select id="email_permission" name="email_permission">{_options(EMAIL_PERMISSIONS, contact.get("email_permission"), empty="Unspecified")}</select></div>
      <div class="field"><label for="company_id">Company</label><select id="company_id" name="company_id">{_company_options(companies, contact.get("company_id"))}</select></div>
      <fieldset class="field"><legend>Buying roles</legend>{_role_checkboxes(roles)}</fieldset>
      <div class="field"><label for="last_interaction_at">Last interaction</label><input id="last_interaction_at" name="last_interaction_at" type="date" value="{_esc(contact.get("last_interaction_at"))}" /></div>
      <div class="field"><label for="relationship_strength">Relationship strength</label><select id="relationship_strength" name="relationship_strength">{_options(RELATIONSHIP_STRENGTHS, contact.get("relationship_strength"), empty="Unspecified")}</select></div>
      <div class="field"><label for="notes">Notes</label><textarea id="notes" name="notes" rows="5" maxlength="10000">{_esc(contact.get("notes"))}</textarea></div>
      <button class="cta admin-submit" type="submit">Save contact</button>
    </form>"""


def render_contacts_list_page(
    *,
    contacts: list[dict[str, Any]],
    companies: list[dict[str, Any]],
    filters: dict[str, str | None],
    csrf_token: str,
    admin_username: str,
) -> str:
    company_names = {str(row["id"]): row.get("name", "") for row in companies}
    rows = "".join(
        f"""<tr>
          <td><a href="/admin/contacts/{_esc(row["id"])}">{_esc(row.get("full_name") or row.get("email") or "Contact")}</a></td>
          <td>{_esc(row.get("title") or "—")}</td>
          <td>{_esc(format_buying_roles(row.get("buying_roles")))}</td>
          <td>{_esc(row.get("company_name") or company_names.get(str(row.get("company_id")), "—"))}</td>
          <td>{_esc(row.get("email") or "—")}</td>
          <td>{_esc(row.get("last_interaction_at") or "Never")}</td>
        </tr>"""
        for row in contacts
    ) or '<tr><td colspan="6">No contacts match these filters.</td></tr>'
    main = f"""<section class="admin-section" aria-labelledby="contacts-title">
      <div class="admin-section-head"><div><p class="admin-eyebrow">CRM</p><h1 class="admin-title" id="contacts-title">Contacts</h1></div><a class="cta" href="/admin/contacts/new">Add contact</a></div>
      <form class="admin-form" method="get" action="/admin/contacts">
        <div class="field"><label for="q">Search</label><input id="q" name="q" value="{_esc(filters.get("q"))}" placeholder="Name, email, title, or profile URL" /></div>
        <div class="field"><label for="company-filter">Company</label><select id="company-filter" name="company_id">{_company_options(companies, filters.get("company_id"))}</select></div>
        <div class="field"><label for="role-filter">Buying role</label><select id="role-filter" name="buying_role">{_options(BUYING_ROLES, filters.get("buying_role"))}</select></div>
        <div class="field"><label><input type="checkbox" name="archived" value="1"{" checked" if filters.get("archived") else ""} /> Include archived</label></div>
        <button class="cta admin-submit" type="submit">Filter</button>
      </form>
      <div class="admin-table-wrap"><table class="admin-table"><thead><tr><th>Name</th><th>Title</th><th>Roles</th><th>Company</th><th>Email</th><th>Last touch</th></tr></thead><tbody>{rows}</tbody></table></div>
    </section>"""
    return render_admin_shell(title="Contacts", main=main, active_path="/admin/contacts", admin_username=admin_username, csrf_token=csrf_token)


def render_contact_form_page(
    *,
    csrf_token: str,
    admin_username: str,
    companies: list[dict[str, Any]],
    contact: dict[str, Any] | None = None,
    error_message: str | None = None,
    warnings: list[Any] | None = None,
) -> str:
    is_new = contact is None
    action = "/admin/contacts" if is_new else f"/admin/contacts/{contact['id']}/edit"
    title = "Add contact" if is_new else f"Edit {contact.get('full_name', '')}"
    warning_html = "".join(
        f'<li>Possible duplicate ({_esc(item.match_type)}): '
        f'<a href="/admin/contacts/{_esc(item.contact_id)}">{_esc(item.label)}</a>.</li>'
        for item in (warnings or [])
    )
    archive_html = ""
    if contact is not None:
        archive_action = "restore" if contact.get("archived_at") else "archive"
        archive_label = "Restore contact" if contact.get("archived_at") else "Archive contact"
        archive_classes = admin_archive_action_classes(archived=bool(contact.get("archived_at")))
        archive_html = f"""<form method="post" action="/admin/contacts/{_esc(contact["id"])}/{archive_action}">
        <input type="hidden" name="csrf_token" value="{_esc(csrf_token)}" />
        <button class="{archive_classes}" type="submit">{archive_label}</button>
      </form>"""
    main = f"""<section class="admin-section" aria-labelledby="contact-form-title">
      <p class="admin-breadcrumb"><a href="/admin/contacts">Contacts</a></p>
      <h1 class="admin-title" id="contact-form-title">{_esc(title)}</h1>
      {'<p class="form-error" role="alert">' + _esc(error_message) + '</p>' if error_message else ''}
      {'<ul class="form-error" role="status">' + warning_html + '</ul>' if warning_html else ''}
      {_contact_form(action=action, csrf_token=csrf_token, companies=companies, contact=contact)}
      {archive_html}
    </section>"""
    return render_admin_shell(title=title, main=main, active_path="/admin/contacts", admin_username=admin_username, csrf_token=csrf_token)


def _safe_field(label: str, value: Any) -> str:
    display = _esc(value) if value else "—"
    return f"<div class=\"brief-detail-row\"><dt>{_esc(label)}</dt><dd>{display}</dd></div>"


def render_contact_restore_conflict_page(
    *,
    csrf_token: str,
    admin_username: str,
    archived_contact: dict[str, Any],
    conflicting_contact: ContactSafeSummary,
    company_name: str | None = None,
) -> str:
    archived_id = str(archived_contact["id"])
    archived_name = archived_contact.get("full_name") or "Archived contact"
    archived_company = company_name or archived_contact.get("company_name") or "—"
    active_label = conflicting_contact.full_name or "Active contact"
    active_company = conflicting_contact.company_name or "—"
    main = f"""<section class="admin-section" aria-labelledby="restore-conflict-title">
      <p class="admin-breadcrumb"><a href="/admin/contacts">Contacts</a> · <a href="/admin/contacts/{_esc(archived_id)}/edit">Edit {_esc(archived_name)}</a></p>
      <p class="admin-eyebrow">CRM</p>
      <h1 class="admin-title" id="restore-conflict-title">Restore blocked — email already in use</h1>
      <p class="admin-lede">
        Another active contact already uses this email address. Change or clear the archived
        contact&apos;s email before restoring. Records are never merged automatically.
      </p>
      <section class="brief-detail-section" aria-labelledby="restore-archived-title">
        <h2 class="brief-detail-heading" id="restore-archived-title">Archived contact</h2>
        <dl class="brief-detail-dl">
          {_safe_field("Name", archived_name)}
          {_safe_field("Title", archived_contact.get("title"))}
          {_safe_field("Company", archived_company)}
          {_safe_field("Email", archived_contact.get("email"))}
        </dl>
        <p><a class="cta" href="/admin/contacts/{_esc(archived_id)}/edit">Edit archived contact</a></p>
      </section>
      <section class="brief-detail-section" aria-labelledby="restore-active-title">
        <h2 class="brief-detail-heading" id="restore-active-title">Active contact using this email</h2>
        <dl class="brief-detail-dl">
          {_safe_field("Name", active_label)}
          {_safe_field("Title", conflicting_contact.title)}
          {_safe_field("Company", active_company)}
        </dl>
        <p><a class="cta" href="/admin/contacts/{_esc(conflicting_contact.contact_id)}/edit">View active contact</a></p>
      </section>
      <p class="admin-lede">The archived contact stays archived until its email no longer conflicts.</p>
    </section>"""
    return render_admin_shell(
        title="Restore blocked",
        main=main,
        active_path="/admin/contacts",
        admin_username=admin_username,
        csrf_token=csrf_token,
    )
