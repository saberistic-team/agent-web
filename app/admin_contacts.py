"""Admin contact list, forms, and company association views."""

from __future__ import annotations

import html
from datetime import datetime
from typing import Any
from uuid import UUID

from app import contacts as contacts_module
from app.admin_layout import render_admin_shell
from app.crm_service import CrmService


def _esc(value: Any) -> str:
    if value is None:
        return ""
    return html.escape(str(value))


def _format_roles(roles: list[str]) -> str:
    if not roles:
        return "—"
    labels = [contacts_module.BUYING_ROLE_LABELS.get(role, role) for role in roles]
    return ", ".join(labels)


def _format_datetime(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    return str(value)[:10]


def _role_checkboxes(selected: list[str]) -> str:
    items: list[str] = []
    for role in contacts_module.BUYING_ROLES:
        label = contacts_module.BUYING_ROLE_LABELS[role]
        checked = " checked" if role in selected else ""
        items.append(
            f"""            <label class="admin-check">
              <input type="checkbox" name="buying_roles" value="{role}"{checked} />
              <span>{html.escape(label)}</span>
            </label>"""
        )
    return "\n".join(items)


def _company_options(companies: list[dict[str, Any]], selected_id: str | None) -> str:
    options = ['            <option value="">— No company —</option>']
    for company in companies:
        company_id = str(company["id"])
        selected = " selected" if selected_id == company_id else ""
        name = _esc(company.get("name"))
        options.append(f'            <option value="{company_id}"{selected}>{name}</option>')
    return "\n".join(options)


def _warnings_block(warnings: list[str]) -> str:
    if not warnings:
        return ""
    items = "\n".join(f"            <li>{_esc(w)}</li>" for w in warnings)
    return f"""          <div class="admin-warnings" role="alert">
            <p class="admin-warnings-title">Possible duplicates</p>
            <ul>
{items}
            </ul>
          </div>"""


def render_contacts_list_page(
    *,
    contacts: list[dict[str, Any]],
    query: str,
    include_archived: bool,
    warnings: list[str] | None = None,
    csrf_token: str | None = None,
) -> str:
    archived_checked = " checked" if include_archived else ""
    rows: list[str] = []
    for contact in contacts:
        contact_id = _esc(contact["id"])
        name = _esc(contacts_module.contact_display_name(contact))
        title = _esc(contact.get("title"))
        company = _esc(contact.get("company_name") or "—")
        roles = _esc(_format_roles(contact.get("buying_roles", [])))
        archived = "Yes" if contact.get("is_archived") else "No"
        rows.append(
            f"""            <tr>
              <td><a href="/admin/contacts/{contact_id}">{name}</a></td>
              <td>{title or "—"}</td>
              <td>{company}</td>
              <td>{roles}</td>
              <td>{archived}</td>
            </tr>"""
        )
    table_body = "\n".join(rows) if rows else """            <tr>
              <td colspan="5">No contacts found.</td>
            </tr>"""
    warning_html = _warnings_block(warnings or [])
    main = f"""        <section class="admin-section" aria-labelledby="contacts-title">
          <div class="admin-section-head">
            <div>
              <p class="admin-eyebrow">CRM</p>
              <h1 class="admin-title" id="contacts-title">Contacts</h1>
            </div>
            <a class="admin-button" href="/admin/contacts/new">New contact</a>
          </div>
{warning_html}
          <form class="admin-toolbar" method="get" action="/admin/contacts">
            <label class="admin-field admin-field-inline">
              <span class="admin-label">Search</span>
              <input type="search" name="q" value="{_esc(query)}" placeholder="Name, email, title, company" />
            </label>
            <label class="admin-check admin-check-inline">
              <input type="checkbox" name="include_archived" value="1"{archived_checked} />
              <span>Include archived</span>
            </label>
            <button type="submit" class="admin-button admin-button-secondary">Search</button>
          </form>
          <div class="admin-table-wrap">
            <table class="admin-table">
              <thead>
                <tr>
                  <th scope="col">Name</th>
                  <th scope="col">Title</th>
                  <th scope="col">Company</th>
                  <th scope="col">Buying roles</th>
                  <th scope="col">Archived</th>
                </tr>
              </thead>
              <tbody>
{table_body}
              </tbody>
            </table>
          </div>
        </section>"""
    return render_admin_shell(title="Contacts", main=main, active_path="/admin/contacts", csrf_token=csrf_token)


def render_contact_form_page(
    *,
    companies: list[dict[str, Any]],
    contact: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
    error: str | None = None,
    is_new: bool = False,
    csrf_token: str | None = None,
) -> str:
    contact = contact or {}
    contact_id = contact.get("id")
    title_label = "New contact" if is_new else "Edit contact"
    form_action = "/admin/contacts/new" if is_new else f"/admin/contacts/{_esc(contact_id)}"
    method = "post"
    name = _esc(contact.get("name"))
    job_title = _esc(contact.get("title"))
    profile_url = _esc(contact.get("profile_url"))
    email = _esc(contact.get("email"))
    email_permission = contact.get("email_permission") or ""
    email_provenance = _esc(contact.get("email_provenance"))
    relationship = contact.get("relationship_strength") or ""
    notes = _esc(contact.get("notes"))
    last_interaction = _format_datetime(contact.get("last_interaction_at"))
    selected_company = str(contact.get("company_id")) if contact.get("company_id") else None
    selected_roles = contact.get("buying_roles", [])
    is_archived = bool(contact.get("is_archived"))

    permission_options = []
    for perm in contacts_module.EMAIL_PERMISSIONS:
        selected = " selected" if email_permission == perm else ""
        label = contacts_module.EMAIL_PERMISSION_LABELS[perm]
        permission_options.append(
            f'              <option value="{perm}"{selected}>{html.escape(label)}</option>'
        )

    strength_options = ['              <option value="">—</option>']
    for strength in contacts_module.RELATIONSHIP_STRENGTHS:
        selected = " selected" if relationship == strength else ""
        label = contacts_module.RELATIONSHIP_STRENGTH_LABELS[strength]
        strength_options.append(
            f'              <option value="{strength}"{selected}>{html.escape(label)}</option>'
        )

    error_html = ""
    if error:
        error_html = f'          <p class="admin-error" role="alert">{_esc(error)}</p>'
    warning_html = _warnings_block(warnings or [])
    csrf_field = ""
    if csrf_token:
        csrf_field = f'            <input type="hidden" name="csrf_token" value="{html.escape(csrf_token, quote=True)}" />\n'

    archive_block = ""
    if not is_new and contact_id:
        if is_archived:
            archive_block = f"""          <p class="admin-note">This contact is archived.</p>
          <button type="submit" formaction="/admin/contacts/{_esc(contact_id)}/restore" class="admin-button admin-button-secondary">Restore</button>"""
        else:
            archive_block = f"""          <button type="submit" formaction="/admin/contacts/{_esc(contact_id)}/archive" class="admin-button admin-button-secondary">Archive</button>"""

    main = f"""        <section class="admin-section" aria-labelledby="contact-form-title">
          <p class="admin-eyebrow">CRM</p>
          <h1 class="admin-title" id="contact-form-title">{html.escape(title_label)}</h1>
{error_html}
{warning_html}
          <form class="admin-form" method="{method}" action="{form_action}">
{csrf_field}            <fieldset class="admin-fieldset">
              <legend>Identity</legend>
              <label class="admin-field">
                <span class="admin-label">Name</span>
                <input type="text" name="name" value="{name}" required maxlength="200" />
              </label>
              <label class="admin-field">
                <span class="admin-label">Title</span>
                <input type="text" name="title" value="{job_title}" maxlength="200" />
              </label>
              <label class="admin-field">
                <span class="admin-label">Profile URL</span>
                <input type="url" name="profile_url" value="{profile_url}" maxlength="500" placeholder="https://linkedin.com/in/…" />
              </label>
              <label class="admin-field">
                <span class="admin-label">Company</span>
                <select name="company_id">
{_company_options(companies, selected_company)}
                </select>
              </label>
            </fieldset>
            <fieldset class="admin-fieldset">
              <legend>Email (optional)</legend>
              <label class="admin-field">
                <span class="admin-label">Email</span>
                <input type="email" name="email" value="{email}" maxlength="320" />
              </label>
              <label class="admin-field">
                <span class="admin-label">Permission</span>
                <select name="email_permission">
                  <option value="">—</option>
{chr(10).join(permission_options)}
                </select>
              </label>
              <label class="admin-field">
                <span class="admin-label">Provenance</span>
                <input type="text" name="email_provenance" value="{email_provenance}" maxlength="200" placeholder="e.g. LinkedIn export, intro" />
              </label>
            </fieldset>
            <fieldset class="admin-fieldset">
              <legend>Relationship</legend>
              <label class="admin-field">
                <span class="admin-label">Last interaction</span>
                <input type="date" name="last_interaction_at" value="{last_interaction}" />
              </label>
              <label class="admin-field">
                <span class="admin-label">Relationship strength</span>
                <select name="relationship_strength">
{chr(10).join(strength_options)}
                </select>
              </label>
              <label class="admin-field">
                <span class="admin-label">Notes</span>
                <textarea name="notes" rows="4" maxlength="5000">{notes}</textarea>
              </label>
            </fieldset>
            <fieldset class="admin-fieldset">
              <legend>Buying roles</legend>
              <div class="admin-check-group">
{_role_checkboxes(selected_roles)}
              </div>
            </fieldset>
            <div class="admin-form-actions">
              <button type="submit" class="admin-button">Save contact</button>
              <a class="admin-button admin-button-secondary" href="/admin/contacts">Cancel</a>
{archive_block}
            </div>
          </form>
        </section>"""
    return render_admin_shell(title=title_label, main=main, active_path="/admin/contacts", csrf_token=csrf_token)


def render_company_detail_page(
    *,
    company: dict[str, Any],
    contacts: list[dict[str, Any]],
    csrf_token: str | None = None,
) -> str:
    company_id = _esc(company["id"])
    company_name = _esc(company.get("name"))
    website = _esc(company.get("website") or "—")
    status = _esc(company.get("status") or "—")

    rows: list[str] = []
    for contact in contacts:
        contact_id = _esc(contact["id"])
        name = _esc(contacts_module.contact_display_name(contact))
        title = _esc(contact.get("title") or "—")
        roles = _esc(_format_roles(contact.get("buying_roles", [])))
        strength = _esc(
            contacts_module.RELATIONSHIP_STRENGTH_LABELS.get(
                str(contact.get("relationship_strength") or ""),
                contact.get("relationship_strength") or "—",
            )
        )
        rows.append(
            f"""            <tr>
              <td><a href="/admin/contacts/{contact_id}">{name}</a></td>
              <td>{title}</td>
              <td>{roles}</td>
              <td>{strength}</td>
            </tr>"""
        )
    table_body = "\n".join(rows) if rows else """            <tr>
              <td colspan="4">No contacts associated with this company.</td>
            </tr>"""

    main = f"""        <section class="admin-section" aria-labelledby="company-title">
          <p class="admin-eyebrow">Company</p>
          <h1 class="admin-title" id="company-title">{company_name}</h1>
          <dl class="admin-meta">
            <div><dt>Website</dt><dd>{website}</dd></div>
            <div><dt>Status</dt><dd>{status}</dd></div>
          </dl>
          <div class="admin-section-head">
            <h2 class="admin-subtitle">Associated contacts</h2>
            <a class="admin-button" href="/admin/contacts/new?company_id={company_id}">Add contact</a>
          </div>
          <div class="admin-table-wrap">
            <table class="admin-table">
              <thead>
                <tr>
                  <th scope="col">Name</th>
                  <th scope="col">Title</th>
                  <th scope="col">Buying roles</th>
                  <th scope="col">Relationship</th>
                </tr>
              </thead>
              <tbody>
{table_body}
              </tbody>
            </table>
          </div>
          <p class="admin-note"><a href="/admin/companies">Back to companies</a></p>
        </section>"""
    return render_admin_shell(title=company_name, main=main, active_path="/admin/companies", csrf_token=csrf_token)


def parse_contact_form(
    *,
    name: str,
    title: str = "",
    profile_url: str = "",
    company_id: str = "",
    email: str = "",
    email_permission: str = "",
    email_provenance: str = "",
    last_interaction_at: str = "",
    relationship_strength: str = "",
    notes: str = "",
    buying_roles: list[str] | None = None,
) -> dict[str, Any]:
    parsed_company: UUID | None = None
    if company_id.strip():
        parsed_company = UUID(company_id.strip())

    parsed_last_interaction: datetime | None = None
    if last_interaction_at.strip():
        parsed_last_interaction = datetime.fromisoformat(last_interaction_at.strip())

    perm = email_permission.strip() or None
    if perm and perm not in contacts_module.EMAIL_PERMISSIONS:
        perm = None

    strength = relationship_strength.strip() or None
    if strength and strength not in contacts_module.RELATIONSHIP_STRENGTHS:
        strength = None

    return {
        "name": name.strip(),
        "title": title.strip() or None,
        "profile_url": profile_url.strip() or None,
        "company_id": parsed_company,
        "email": email.strip() or None,
        "email_permission": perm,
        "email_provenance": email_provenance.strip() or None,
        "last_interaction_at": parsed_last_interaction,
        "relationship_strength": strength,
        "notes": notes.strip() or None,
        "buying_roles": contacts_module.parse_buying_roles(buying_roles or []),
    }


def load_contacts_context(
    crm: CrmService,
    conn: Any,
) -> list[dict[str, Any]]:
    return crm._repos.companies.list_all(conn)  # type: ignore[attr-defined]
