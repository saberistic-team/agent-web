"""Admin HTML for contact record management."""

from __future__ import annotations

import html
from typing import Any

from app.admin_layout import render_admin_shell
from app.contacts import (
    BUYING_ROLES,
    EMAIL_PROVENANCES,
    RELATIONSHIP_STRENGTHS,
    format_last_interaction,
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
    rows = ['<option value="">Unassigned</option>']
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
    contact: dict[str, Any] | None = None,
    companies: list[dict[str, Any]] | None = None,
) -> str:
    contact = contact or {}
    companies = companies or []
    return f"""<form class="admin-form" method="post" action="{_esc(action)}">
      <input type="hidden" name="csrf_token" value="{_esc(csrf_token)}" />
      <div class="field"><label for="full_name">Name</label><input id="full_name" name="full_name" required maxlength="500" value="{_esc(contact.get("full_name"))}" /></div>
      <div class="field"><label for="title">Title</label><input id="title" name="title" maxlength="500" value="{_esc(contact.get("title"))}" /></div>
      <div class="field"><label for="profile_url">LinkedIn / profile URL</label><input id="profile_url" name="profile_url" type="url" maxlength="2000" placeholder="https://linkedin.com/in/..." value="{_esc(contact.get("profile_url"))}" /></div>
      <div class="field"><label for="company_id">Company</label><select id="company_id" name="company_id">{_company_options(companies, contact.get("company_id"))}</select></div>
      <div class="field"><label for="email">Email</label><input id="email" name="email" type="email" maxlength="320" value="{_esc(contact.get("email"))}" /></div>
      <div class="field"><label><input type="checkbox" name="email_permitted" value="1"{" checked" if contact.get("email_permitted") else ""} /> Email permitted for outreach</label></div>
      <div class="field"><label for="email_provenance">Email provenance</label><select id="email_provenance" name="email_provenance">{_options(EMAIL_PROVENANCES, contact.get("email_provenance"), empty="Unspecified")}</select></div>
      <div class="field"><label for="last_interaction_at">Last interaction</label><input id="last_interaction_at" name="last_interaction_at" type="date" value="{_esc(contact.get("last_interaction_at"))}" /></div>
      <div class="field"><label for="relationship_strength">Relationship strength</label><select id="relationship_strength" name="relationship_strength">{_options(RELATIONSHIP_STRENGTHS, contact.get("relationship_strength"), empty="Unspecified")}</select></div>
      <fieldset class="field"><legend>Buying roles</legend>{_role_checkboxes(contact.get("buying_roles"))}</fieldset>
      <div class="field"><label for="notes">Notes</label><textarea id="notes" name="notes" rows="5" maxlength="10000">{_esc(contact.get("notes"))}</textarea></div>
      <button class="cta admin-submit" type="submit">Save contact</button>
    </form>"""


def _role_labels(roles: list[str] | None) -> str:
    if not roles:
        return "—"
    return ", ".join(BUYING_ROLES.get(role, role) for role in roles)


def render_contacts_list_page(
    *,
    contacts: list[dict[str, Any]],
    companies_by_id: dict[str, dict[str, Any]],
    filters: dict[str, str | None],
    csrf_token: str,
    admin_username: str,
) -> str:
    rows = "".join(
        f"""<tr>
          <td><a href="/admin/contacts/{_esc(row["id"])}">{_esc(row.get("full_name") or row.get("email") or row["id"])}</a></td>
          <td>{_esc(row.get("title") or "—")}</td>
          <td>{_esc(companies_by_id.get(str(row.get("company_id")), {}).get("name") or "—")}</td>
          <td>{_esc(_role_labels(row.get("buying_roles")))}</td>
          <td>{_esc(row.get("email") or "—")}</td>
          <td>{_esc(format_last_interaction(row.get("last_interaction_at")))}</td>
        </tr>"""
        for row in contacts
    ) or '<tr><td colspan="6">No contacts match these filters.</td></tr>'
    company_filter_options = _company_options(
        list(companies_by_id.values()),
        filters.get("company_id"),
    )
    main = f"""<section class="admin-section" aria-labelledby="contacts-title">
      <div class="admin-section-head"><div><p class="admin-eyebrow">CRM</p><h1 class="admin-title" id="contacts-title">Contacts</h1></div><a class="cta" href="/admin/contacts/new">Add contact</a></div>
      <form class="admin-form" method="get" action="/admin/contacts">
        <div class="field"><label for="q">Search</label><input id="q" name="q" value="{_esc(filters.get("q"))}" placeholder="Name, email, title, or profile URL" /></div>
        <div class="field"><label for="company-filter">Company</label><select id="company-filter" name="company_id">{company_filter_options}</select></div>
        <div class="field"><label><input type="checkbox" name="archived" value="1"{" checked" if filters.get("archived") else ""} /> Include archived</label></div>
        <button class="cta admin-submit" type="submit">Filter</button>
      </form>
      <div class="admin-table-wrap"><table class="admin-table"><thead><tr><th>Name</th><th>Title</th><th>Company</th><th>Roles</th><th>Email</th><th>Last touch</th></tr></thead><tbody>{rows}</tbody></table></div>
    </section>"""
    return render_admin_shell(
        title="Contacts",
        main=main,
        active_path="/admin/contacts",
        admin_username=admin_username,
        csrf_token=csrf_token,
    )


def render_contact_form_page(
    *,
    csrf_token: str,
    admin_username: str,
    contact: dict[str, Any] | None = None,
    companies: list[dict[str, Any]] | None = None,
    error_message: str | None = None,
    warnings: list[Any] | None = None,
) -> str:
    is_new = contact is None
    action = "/admin/contacts" if is_new else f"/admin/contacts/{contact['id']}/edit"
    title = "Add contact" if is_new else f"Edit {contact.get('full_name', '')}"
    warning_html = "".join(
        f'<li>{_esc(item.reason)} match: <a href="/admin/contacts/{_esc(item.contact_id)}">{_esc(item.full_name)}</a> ({_esc(item.detail)}).</li>'
        for item in (warnings or [])
    )
    main = f"""<section class="admin-section" aria-labelledby="contact-form-title">
      <p class="admin-breadcrumb"><a href="/admin/contacts">Contacts</a></p>
      <h1 class="admin-title" id="contact-form-title">{_esc(title)}</h1>
      {'<p class="form-error" role="alert">' + _esc(error_message) + '</p>' if error_message else ''}
      {'<ul class="form-error" role="status">' + warning_html + '</ul>' if warning_html else ''}
      {_contact_form(action=action, csrf_token=csrf_token, contact=contact, companies=companies)}
    </section>"""
    return render_admin_shell(
        title=title,
        main=main,
        active_path="/admin/contacts",
        admin_username=admin_username,
        csrf_token=csrf_token,
    )
