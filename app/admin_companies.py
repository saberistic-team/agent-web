"""Admin company list and detail pages with associated contacts."""

from __future__ import annotations

import html
from typing import Any

from app.admin_layout import render_admin_shell


def _esc(value: Any) -> str:
    if value is None:
        return ""
    return html.escape(str(value))


def render_companies_list_page(
    *,
    admin_username: str,
    companies: list[dict[str, Any]],
    csrf_token: str = "",
) -> str:
    rows: list[str] = []
    for company in companies:
        company_id = _esc(company["id"])
        name = _esc(company.get("name"))
        website = _esc(company.get("website") or "—")
        status = _esc(company.get("status") or "—")
        rows.append(
            f"""            <tr>
              <td><a href="/admin/companies/{company_id}">{name}</a></td>
              <td>{website}</td>
              <td>{status}</td>
            </tr>"""
        )
    table_body = "\n".join(rows) if rows else """            <tr>
              <td colspan="3">No companies yet. Create one when adding a contact.</td>
            </tr>"""
    main = f"""        <section class="admin-section" aria-labelledby="companies-title">
          <div class="admin-section-head">
            <div>
              <p class="admin-eyebrow">CRM</p>
              <h1 class="admin-title" id="companies-title">Companies</h1>
            </div>
          </div>
          <p class="admin-lede">Open a company to view associated contacts.</p>
          <div class="admin-table-wrap">
            <table class="admin-table">
              <thead>
                <tr>
                  <th scope="col">Name</th>
                  <th scope="col">Website</th>
                  <th scope="col">Status</th>
                </tr>
              </thead>
              <tbody>
{table_body}
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
