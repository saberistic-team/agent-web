"""Admin HTML for LinkedIn export import preview (browser-local parsing)."""

from __future__ import annotations

import html
import json

from app.admin_layout import render_admin_shell
from app.linkedin_export_parser import export_limits_for_client


def _esc(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def render_imports_page(*, admin_username: str, csrf_token: str) -> str:
    """Return the authenticated admin imports page (ZIP stays in-browser)."""
    limits_json = json.dumps(export_limits_for_client())
    main = f"""<section class="admin-section linkedin-import" aria-labelledby="imports-title">
      <div class="admin-section-head">
        <div>
          <p class="admin-eyebrow">Data import</p>
          <h1 class="admin-title" id="imports-title">LinkedIn export preview</h1>
        </div>
      </div>
      <p class="admin-lede">
        Select an official LinkedIn data-export ZIP. Parsing runs entirely in your browser;
        the raw archive is never uploaded and message bodies are not transmitted.
      </p>

      <div class="linkedin-import-privacy" role="note">
        <h2 class="admin-section-title">Privacy &amp; retention</h2>
        <dl class="linkedin-import-privacy-grid">
          <div>
            <dt>Processed locally</dt>
            <dd>
              <code>Connections.csv</code>, <code>messages.csv</code>,
              <code>Invitations.csv</code>, and <code>Company Follows.csv</code>
            </dd>
          </div>
          <div>
            <dt>Ignored in archive</dt>
            <dd>
              Logins, security challenges, phones, job answers, ads, verification,
              receipts, and all other files
            </dd>
          </div>
          <div>
            <dt>Transmitted</dt>
            <dd>Nothing in this preview step — no network upload of the ZIP or message text</dd>
          </div>
          <div>
            <dt>Retained</dt>
            <dd>Nothing server-side until a future import-batch step you explicitly confirm</dd>
          </div>
        </dl>
      </div>

      <form class="admin-form admin-form--editor linkedin-import-form" id="linkedin-import-form">
        <div class="field">
          <label for="linkedin-export-zip">LinkedIn export ZIP</label>
          <input
            id="linkedin-export-zip"
            name="linkedin_export_zip"
            type="file"
            accept=".zip,application/zip"
          />
          <p class="admin-note">
            Max {_esc(export_limits_for_client()["maxCompressedBytes"] // (1024 * 1024))} MiB compressed;
            {_esc(export_limits_for_client()["maxUncompressedBytes"] // (1024 * 1024))} MiB uncompressed total.
          </p>
        </div>
      </form>

      <div id="linkedin-import-status" class="linkedin-import-status" role="status" aria-live="polite"></div>
      <div id="linkedin-import-preview" class="linkedin-import-preview" hidden></div>

      <script type="application/json" id="linkedin-import-limits">{limits_json}</script>
      <script src="/assets/linkedin-import.js" defer></script>
    </section>"""
    return render_admin_shell(
        title="Imports",
        main=main,
        active_path="/admin/imports",
        admin_username=admin_username,
        csrf_token=csrf_token,
    )
