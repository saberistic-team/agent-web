"""Admin HTML for company record management."""

from __future__ import annotations

import html
from typing import Any

from app.admin_layout import render_admin_shell
from app.companies import COMPANY_CATEGORIES, COMPANY_STAGES, FRESHNESS_FILTERS, TARGET_STATUSES


def _esc(value: Any) -> str:
    return "" if value is None else html.escape(str(value), quote=True)


def _options(registry: dict[str, str], selected: str | None, *, empty: str = "Any") -> str:
    rows = [f'<option value="">{html.escape(empty)}</option>']
    rows.extend(
        f'<option value="{_esc(key)}"{" selected" if key == selected else ""}>{_esc(label)}</option>'
        for key, label in registry.items()
    )
    return "\n".join(rows)


def _company_form(
    *,
    action: str,
    csrf_token: str,
    company: dict[str, Any] | None = None,
) -> str:
    company = company or {}
    return f"""<form class="admin-form" method="post" action="{_esc(action)}">
      <input type="hidden" name="csrf_token" value="{_esc(csrf_token)}" />
      <div class="field"><label for="name">Name</label><input id="name" name="name" required maxlength="500" value="{_esc(company.get("name"))}" /></div>
      <div class="field"><label for="domain">Domain</label><input id="domain" name="domain" maxlength="253" placeholder="example.com" value="{_esc(company.get("domain"))}" /></div>
      <div class="field"><label for="website">Website</label><input id="website" name="website" type="url" maxlength="2000" value="{_esc(company.get("website"))}" /></div>
      <div class="field"><label for="category">Category</label><select id="category" name="category">{_options(COMPANY_CATEGORIES, company.get("category"), empty="Unspecified")}</select></div>
      <div class="field"><label for="stage">Stage</label><select id="stage" name="stage">{_options(COMPANY_STAGES, company.get("stage"), empty="Unspecified")}</select></div>
      <div class="field"><label for="headcount_estimate">Headcount estimate</label><input id="headcount_estimate" name="headcount_estimate" type="number" min="0" value="{_esc(company.get("headcount_estimate"))}" /></div>
      <div class="field"><label for="funding_summary">Funding summary</label><input id="funding_summary" name="funding_summary" maxlength="2000" value="{_esc(company.get("funding_summary"))}" /></div>
      <div class="field"><label for="target_status">Target status</label><select id="target_status" name="target_status">{_options(TARGET_STATUSES, company.get("target_status"), empty="Unspecified")}</select></div>
      <div class="field"><label for="last_verified_at">Last verified</label><input id="last_verified_at" name="last_verified_at" type="date" value="{_esc(company.get("last_verified_at"))}" /></div>
      <div class="field"><label for="notes">Notes</label><textarea id="notes" name="notes" rows="5" maxlength="10000">{_esc(company.get("notes"))}</textarea></div>
      <button class="cta admin-submit" type="submit">Save company</button>
    </form>"""


def render_companies_list_page(
    *,
    companies: list[dict[str, Any]],
    filters: dict[str, str | None],
    csrf_token: str,
    admin_username: str,
    preview_banner: str | None = None,
) -> str:
    banner_html = ""
    if preview_banner:
        banner_html = (
            f'<p class="admin-preview-banner" role="status">{_esc(preview_banner)}</p>'
        )
    rows = "".join(
        f"""<tr>
          <td><a href="/admin/companies/{_esc(row["id"])}">{_esc(row.get("name"))}</a></td>
          <td>{_esc(COMPANY_CATEGORIES.get(str(row.get("category")), row.get("category") or "—"))}</td>
          <td>{_esc(COMPANY_STAGES.get(str(row.get("stage")), row.get("stage") or "—"))}</td>
          <td>{_esc(TARGET_STATUSES.get(str(row.get("target_status")), row.get("target_status") or "—"))}</td>
          <td>{_esc(row.get("last_verified_at") or "Never")}</td>
        </tr>"""
        for row in companies
    ) or '<tr><td colspan="5">No companies match these filters.</td></tr>'
    main = f"""<section class="admin-section" aria-labelledby="companies-title">
      {banner_html}
      <div class="admin-section-head"><div><p class="admin-eyebrow">CRM</p><h1 class="admin-title" id="companies-title">Companies</h1></div><a class="cta" href="/admin/companies/new">Add company</a></div>
      <form class="admin-form" method="get" action="/admin/companies">
        <div class="field"><label for="q">Search</label><input id="q" name="q" value="{_esc(filters.get("q"))}" placeholder="Name or domain" /></div>
        <div class="field"><label for="category-filter">Category</label><select id="category-filter" name="category">{_options(COMPANY_CATEGORIES, filters.get("category"))}</select></div>
        <div class="field"><label for="stage-filter">Stage</label><select id="stage-filter" name="stage">{_options(COMPANY_STAGES, filters.get("stage"))}</select></div>
        <div class="field"><label for="target-filter">Target status</label><select id="target-filter" name="target_status">{_options(TARGET_STATUSES, filters.get("target_status"))}</select></div>
        <div class="field"><label for="freshness-filter">Freshness</label><select id="freshness-filter" name="freshness">{_options(FRESHNESS_FILTERS, filters.get("freshness"))}</select></div>
        <div class="field"><label><input type="checkbox" name="archived" value="1"{" checked" if filters.get("archived") else ""} /> Include archived</label></div>
        <button class="cta admin-submit" type="submit">Filter</button>
      </form>
      <div class="admin-table-wrap"><table class="admin-table"><thead><tr><th>Name</th><th>Category</th><th>Stage</th><th>Target</th><th>Verified</th></tr></thead><tbody>{rows}</tbody></table></div>
    </section>"""
    return render_admin_shell(title="Companies", main=main, active_path="/admin/companies", admin_username=admin_username, csrf_token=csrf_token)


def render_company_form_page(
    *,
    csrf_token: str,
    admin_username: str,
    company: dict[str, Any] | None = None,
    error_message: str | None = None,
    warnings: list[Any] | None = None,
) -> str:
    is_new = company is None
    action = "/admin/companies" if is_new else f"/admin/companies/{company['id']}/edit"
    title = "Add company" if is_new else f"Edit {company.get('name', '')}"
    warning_html = "".join(
        f'<li>Domain already belongs to <a href="/admin/companies/{_esc(item.company_id)}">{_esc(item.name)}</a>.</li>'
        for item in (warnings or [])
    )
    main = f"""<section class="admin-section" aria-labelledby="company-form-title">
      <p class="admin-breadcrumb"><a href="/admin/companies">Companies</a></p>
      <h1 class="admin-title" id="company-form-title">{_esc(title)}</h1>
      {'<p class="form-error" role="alert">' + _esc(error_message) + '</p>' if error_message else ''}
      {'<ul class="form-error" role="status">' + warning_html + '</ul>' if warning_html else ''}
      {_company_form(action=action, csrf_token=csrf_token, company=company)}
    </section>"""
    return render_admin_shell(title=title, main=main, active_path="/admin/companies", admin_username=admin_username, csrf_token=csrf_token)
