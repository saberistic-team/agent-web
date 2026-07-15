"""Admin HTML for LinkedIn export import preview."""

from __future__ import annotations

import html
import json
from typing import Any

from app.admin_layout import render_admin_shell


def _esc(value: Any) -> str:
    return "" if value is None else html.escape(str(value), quote=True)


def _render_privacy_panel() -> str:
    return """      <section class="admin-import-privacy" aria-labelledby="import-privacy-title">
        <h2 class="admin-subtitle" id="import-privacy-title">Privacy and data handling</h2>
        <dl class="admin-import-policy">
          <div>
            <dt>Processed locally</dt>
            <dd>Connections, invitations, company follows, and message metadata from your official LinkedIn export ZIP.</dd>
          </div>
          <div>
            <dt>Ignored</dt>
            <dd>Logins, security challenges, phone numbers, job answers, ads, verification artifacts, receipts, and every other archive file.</dd>
          </div>
          <div>
            <dt>Transmitted</dt>
            <dd>Nothing in the default flow — parsing runs entirely in your browser. No upload request is sent.</dd>
          </div>
          <div>
            <dt>Retained</dt>
            <dd>Nothing — the raw ZIP and message bodies are not stored on the server during preview. Import commit ships in a later issue.</dd>
          </div>
        </dl>
      </section>"""


def _render_upload_panel(*, disabled: bool = False) -> str:
    disabled_attr = " disabled" if disabled else ""
    return f"""      <section class="admin-import-upload" aria-labelledby="import-upload-title">
        <h2 class="admin-subtitle" id="import-upload-title">Upload LinkedIn export</h2>
        <p class="admin-note">Choose the official <code>.zip</code> from LinkedIn&apos;s &ldquo;Download your data&rdquo; export. Parsing stays on this device.</p>
        <div class="field">
          <label for="linkedin-export-file">Export archive (.zip only)</label>
          <input id="linkedin-export-file" name="export" type="file" accept=".zip,application/zip"{disabled_attr} />
        </div>
        <p id="linkedin-import-status" class="admin-import-status" role="status" aria-live="polite"></p>
      </section>"""


def _render_preview_shell(*, hidden: bool = True) -> str:
    hidden_attr = " hidden" if hidden else ""
    return f"""      <section id="linkedin-import-preview" class="admin-import-preview"{hidden_attr} aria-live="polite"></section>"""


def _render_preview_counts(counts: dict[str, int]) -> str:
    return f"""        <div class="admin-import-summary">
          <dl class="admin-stat-grid">
            <div><dt>Connections</dt><dd>{_esc(counts.get("connections", 0))}</dd></div>
            <div><dt>Conversations</dt><dd>{_esc(counts.get("conversations", 0))}</dd></div>
            <div><dt>Messages</dt><dd>{_esc(counts.get("messages", 0))} <span class="admin-note">(content not shown)</span></dd></div>
            <div><dt>Invitations</dt><dd>{_esc(counts.get("invitations", 0))}</dd></div>
            <div><dt>Company follows</dt><dd>{_esc(counts.get("company_follows", 0))}</dd></div>
          </dl>
        </div>"""


def _render_recognized_files(files: list[dict[str, Any]]) -> str:
    items = "".join(
        f"<li><code>{_esc(row['basename'])}</code> — {_esc(row['valid_rows'])} valid rows "
        f"({_esc(row['skipped_rows'])} skipped)</li>"
        for row in files
    )
    return f"""        <h2 class="admin-subtitle">Recognized files</h2>
        <ul class="admin-import-file-list">{items}</ul>"""


def _render_proposed_table(changes: list[dict[str, Any]]) -> str:
    rows: list[str] = []
    for row in changes[:25]:
        if row.get("kind") == "contact":
            rows.append(
                f"<tr><td>Contact</td><td>{_esc(row.get('name'))}</td>"
                f"<td>{_esc(row.get('company') or '—')}</td>"
                f"<td>{_esc(row.get('profile_url') or '—')}</td></tr>"
            )
        elif row.get("kind") == "invitation":
            rows.append(
                f"<tr><td>Invitation</td><td>{_esc(row.get('from') or '—')}</td>"
                f"<td>{_esc(row.get('to') or '—')}</td>"
                f"<td>{_esc(row.get('sent_at') or '—')}</td></tr>"
            )
        else:
            rows.append(
                f"<tr><td>Company follow</td><td>{_esc(row.get('organization'))}</td>"
                f"<td>—</td><td>{_esc(row.get('followed_on') or '—')}</td></tr>"
            )
    body = "".join(rows) or '<tr><td colspan="4">No importable rows detected.</td></tr>'
    return f"""        <h2 class="admin-subtitle">Proposed import preview</h2>
        <p class="admin-note">Sample rows only — full commit ships in a later issue. Message bodies are never displayed or transmitted.</p>
        <div class="admin-table-wrap">
          <table class="admin-table">
            <thead><tr><th>Type</th><th>Primary</th><th>Secondary</th><th>Detail</th></tr></thead>
            <tbody>{body}</tbody>
          </table>
        </div>"""


def render_linkedin_import_page(
    *,
    admin_username: str,
    csrf_token: str = "",
    preview_banner: str | None = None,
    preview_data: dict[str, Any] | None = None,
    include_scripts: bool = True,
) -> str:
    """Render the LinkedIn import admin page."""
    banner_html = ""
    if preview_banner:
        banner_html = (
            f'<p class="admin-preview-banner" role="status">{_esc(preview_banner)}</p>'
        )

    if preview_data is not None:
        preview_section = f"""      <section id="linkedin-import-preview" class="admin-import-preview" aria-live="polite">
        <div class="admin-alert admin-alert-ok" role="status">
          <p class="admin-alert-title">Export parsed locally</p>
          <p class="admin-note">Preview mock data for screenshots — no upload occurred.</p>
        </div>
{_render_preview_counts(preview_data.get("counts", {}))}
{_render_recognized_files(preview_data.get("recognized_files", []))}
        <h2 class="admin-subtitle">Ignored archive contents</h2>
        <p class="admin-note">{_esc(preview_data.get("ignored_file_count", 0))} other file(s) were skipped.</p>
{_render_proposed_table(preview_data.get("proposed_changes", []))}
      </section>"""
        upload_section = _render_upload_panel(disabled=True)
        scripts = ""
    else:
        preview_section = _render_preview_shell(hidden=True)
        upload_section = _render_upload_panel()
        scripts = ""
        if include_scripts:
            scripts = """
    <script src="/assets/fflate.min.js"></script>
    <script src="/assets/linkedin-export.js"></script>"""

    main = f"""        <section class="admin-section" aria-labelledby="imports-title">
          {banner_html}
          <div class="admin-section-head">
            <div>
              <p class="admin-eyebrow">Data import</p>
              <h1 class="admin-title" id="imports-title">Imports</h1>
              <p class="admin-lede">Review a LinkedIn data export locally before any CRM write.</p>
            </div>
          </div>
{_render_privacy_panel()}
{upload_section}
{preview_section}
        </section>"""

    shell = render_admin_shell(
        title="Imports",
        main=main,
        active_path="/admin/imports",
        admin_username=admin_username,
        csrf_token=csrf_token,
    )
    if scripts:
        return shell.replace("</body>", scripts + "\n  </body>")
    return shell


def preview_data_to_json(preview_data: dict[str, Any]) -> str:
    """Serialize preview mock data for embedding in tests."""
    return json.dumps(preview_data, separators=(",", ":"))
