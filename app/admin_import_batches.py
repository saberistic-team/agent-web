"""Admin HTML for persisted LinkedIn import batches."""

from __future__ import annotations

import html
import json
from typing import Any

from app.admin_layout import render_admin_shell
from app.linkedin_import import SOURCE_TYPE_LINKEDIN


def _esc(value: Any) -> str:
    return "" if value is None else html.escape(str(value), quote=True)


def _summary_badges(summary: dict[str, Any] | None) -> str:
    counts = summary or {}
    parts = []
    for key in ("inserted", "updated", "unchanged", "skipped", "conflicted"):
        value = counts.get(key, 0)
        if value:
            parts.append(f"{key} {value}")
    return ", ".join(parts) or "—"


def render_import_batches_page(
    *,
    batches: list[dict[str, Any]],
    page: int,
    per_page: int,
    total: int,
    admin_username: str,
    csrf_token: str,
    preview_banner: str | None = None,
) -> str:
    rows = "".join(
        f"""<tr>
          <td><a href="/admin/imports/batches/{_esc(item["id"])}">{_esc(str(item["id"])[:8])}…</a></td>
          <td>{_esc(item.get("source_type"))}</td>
          <td>{_esc(item.get("export_date") or "—")}</td>
          <td>{_esc(item.get("schema_version"))}</td>
          <td><code>{_esc(str(item.get("checksum", ""))[:12])}…</code></td>
          <td>{_esc(item.get("actor"))}</td>
          <td>{_esc(item.get("status"))}</td>
          <td>{_summary_badges(item.get("summary_counts"))}</td>
          <td>{_esc(item.get("created_at"))}</td>
        </tr>"""
        for item in batches
    ) or '<tr><td colspan="9">No import batches yet.</td></tr>'

    banner = (
        f'<p class="admin-note" role="status">{_esc(preview_banner)}</p>'
        if preview_banner
        else ""
    )
    main = f"""<section class="admin-section" aria-labelledby="import-batches-title">
      <div class="admin-section-head">
        <div>
          <p class="admin-eyebrow">Data import</p>
          <h1 class="admin-title" id="import-batches-title">Import batches</h1>
        </div>
        <a class="cta" href="/admin/imports">LinkedIn preview</a>
      </div>
      {banner}
      <p class="admin-lede">
        Auditable history of committed LinkedIn exports — checksum, actor, status, and per-row outcomes.
      </p>
      <div class="admin-table-wrap">
        <table class="admin-table">
          <thead>
            <tr>
              <th>Batch</th><th>Source</th><th>Export date</th><th>Schema</th>
              <th>Checksum</th><th>Actor</th><th>Status</th><th>Counts</th><th>Committed</th>
            </tr>
          </thead>
          <tbody>{rows}</tbody>
        </table>
      </div>
      <p class="admin-note">Page {page} · {total} total batches · {per_page} per page</p>
    </section>"""
    return render_admin_shell(
        title="Import batches",
        main=main,
        active_path="/admin/imports",
        admin_username=admin_username,
        csrf_token=csrf_token,
    )


def render_import_batch_detail_page(
    *,
    batch: dict[str, Any],
    rows: list[dict[str, Any]],
    admin_username: str,
    csrf_token: str,
    preview_banner: str | None = None,
    rollback_message: str | None = None,
) -> str:
    summary = batch.get("summary_counts") or {}
    stats = "".join(
        f"<div><dt>{_esc(label)}</dt><dd>{summary.get(key, 0)}</dd></div>"
        for key, label in (
            ("inserted", "Inserted"),
            ("updated", "Updated"),
            ("unchanged", "Unchanged"),
            ("skipped", "Skipped"),
            ("conflicted", "Conflicted"),
        )
    )
    row_html = "".join(
        _render_batch_row(row)
        for row in rows
    ) or '<tr><td colspan="5">No row outcomes recorded.</td></tr>'

    rollback_form = ""
    if batch.get("status") == "committed":
        rollback_form = f"""<form class="admin-form admin-form--compact" method="post" action="/admin/imports/batches/{_esc(batch["id"])}/rollback">
          <input type="hidden" name="csrf_token" value="{_esc(csrf_token)}" />
          <button class="cta admin-submit" type="submit">Rollback batch</button>
          <p class="admin-note">Reverts batch-owned inserts and updates when records were not edited later.</p>
        </form>"""

    banner = (
        f'<p class="admin-note" role="status">{_esc(preview_banner)}</p>'
        if preview_banner
        else ""
    )
    message = (
        f'<p class="form-error" role="status">{_esc(rollback_message)}</p>'
        if rollback_message
        else ""
    )

    main = f"""<section class="admin-section" aria-labelledby="import-batch-title">
      <p class="admin-breadcrumb"><a href="/admin/imports/batches">Import batches</a></p>
      <h1 class="admin-title" id="import-batch-title">Batch {_esc(str(batch["id"])[:8])}…</h1>
      {banner}
      {message}
      <dl class="admin-stat-row">{stats}</dl>
      <dl class="admin-meta-grid">
        <div><dt>Source</dt><dd>{_esc(batch.get("source_type") or SOURCE_TYPE_LINKEDIN)}</dd></div>
        <div><dt>Export date</dt><dd>{_esc(batch.get("export_date") or "—")}</dd></div>
        <div><dt>Schema</dt><dd>{_esc(batch.get("schema_version"))}</dd></div>
        <div><dt>Checksum</dt><dd><code>{_esc(batch.get("checksum"))}</code></dd></div>
        <div><dt>Actor</dt><dd>{_esc(batch.get("actor"))}</dd></div>
        <div><dt>Status</dt><dd>{_esc(batch.get("status"))}</dd></div>
        <div><dt>Correlation</dt><dd><code>{_esc(batch.get("correlation_id"))}</code></dd></div>
      </dl>
      {rollback_form}
      <h2 class="admin-section-title">Row outcomes</h2>
      <div class="admin-table-wrap">
        <table class="admin-table">
          <thead>
            <tr><th>#</th><th>Outcome</th><th>Identity</th><th>Entity</th><th>Detail</th></tr>
          </thead>
          <tbody>{row_html}</tbody>
        </table>
      </div>
    </section>"""
    return render_admin_shell(
        title="Import batch",
        main=main,
        active_path="/admin/imports",
        admin_username=admin_username,
        csrf_token=csrf_token,
    )


def _render_batch_row(row: dict[str, Any]) -> str:
    identity = row.get("source_identity") or {}
    if isinstance(identity, str):
        try:
            identity = json.loads(identity)
        except json.JSONDecodeError:
            identity = {}
    identity_bits = []
    if identity.get("full_name"):
        identity_bits.append(str(identity["full_name"]))
    if identity.get("profile_url"):
        identity_bits.append(str(identity["profile_url"]))
    if identity.get("company_name"):
        identity_bits.append(str(identity["company_name"]))
    identity_text = " · ".join(identity_bits) or "—"
    entity = "—"
    if row.get("entity_type") and row.get("entity_id"):
        entity = f'{row["entity_type"]} {str(row["entity_id"])[:8]}…'
    return f"""<tr>
      <td>{_esc(row.get("row_index"))}</td>
      <td>{_esc(row.get("outcome"))}</td>
      <td>{_esc(identity_text)}</td>
      <td>{_esc(entity)}</td>
      <td>{_esc(row.get("detail") or "—")}</td>
    </tr>"""
