"""Admin HTML for discovery run history and manual triggers."""

from __future__ import annotations

import html
import json
from typing import Any

from app.admin_layout import render_admin_shell


def _esc(value: Any) -> str:
    return "" if value is None else html.escape(str(value), quote=True)


def _format_sources(sources: list[str] | None) -> str:
    if not sources:
        return "—"
    return ", ".join(html.escape(source) for source in sources)


def render_discovery_runs_page(
    *,
    runs: list[dict[str, Any]],
    page: int,
    per_page: int,
    total: int,
    admin_username: str,
    csrf_token: str,
    schedule_interval_days: int,
    preview_banner: str | None = None,
    trigger_message: str | None = None,
) -> str:
    rows = "".join(
        f"""<tr>
          <td><a href="/admin/discovery/runs/{_esc(item["id"])}">{_esc(str(item["id"])[:8])}…</a></td>
          <td>{_esc(item.get("trigger_type"))}</td>
          <td>{_esc(item.get("status"))}</td>
          <td>{_format_sources(item.get("enabled_sources"))}</td>
          <td>{_esc(item.get("actor") or "—")}</td>
          <td>{_esc(item.get("started_at"))}</td>
          <td>{_esc(item.get("finished_at") or "—")}</td>
        </tr>"""
        for item in runs
    ) or '<tr><td colspan="7">No discovery runs yet.</td></tr>'

    banner = (
        f'<p class="admin-note" role="status">{_esc(preview_banner)}</p>'
        if preview_banner
        else ""
    )
    message = (
        f'<p class="form-error" role="status">{_esc(trigger_message)}</p>'
        if trigger_message
        else ""
    )

    main = f"""<section class="admin-section" aria-labelledby="discovery-runs-title">
      <div class="admin-section-head">
        <div>
          <p class="admin-eyebrow">Lead discovery</p>
          <h1 class="admin-title" id="discovery-runs-title">Discovery runs</h1>
        </div>
        <a class="cta" href="/admin/discovery/inbox">Review inbox</a>
        <form class="admin-form admin-form--compact" method="post" action="/admin/discovery/run">
          <input type="hidden" name="csrf_token" value="{_esc(csrf_token)}" />
          <button class="cta admin-submit" type="submit">Run discovery now</button>
        </form>
      </div>
      {banner}
      {message}
      <p class="admin-lede">
        Weekly scheduled discovery with per-source checkpoints, locking, and auditable run history.
        Default schedule: every {schedule_interval_days} days (configure via <code>DISCOVERY_SCHEDULE_INTERVAL_DAYS</code>).
      </p>
      <div class="admin-table-wrap">
        <table class="admin-table">
          <thead>
            <tr>
              <th>Run</th><th>Trigger</th><th>Status</th><th>Sources</th>
              <th>Actor</th><th>Started</th><th>Finished</th>
            </tr>
          </thead>
          <tbody>{rows}</tbody>
        </table>
      </div>
      <p class="admin-note">Page {page} · {total} total runs · {per_page} per page</p>
    </section>"""
    return render_admin_shell(
        title="Discovery runs",
        main=main,
        active_path="/admin/discovery",
        admin_username=admin_username,
        csrf_token=csrf_token,
    )


def render_discovery_run_detail_page(
    *,
    run: dict[str, Any],
    sources: list[dict[str, Any]],
    admin_username: str,
    csrf_token: str,
    preview_banner: str | None = None,
) -> str:
    source_rows = "".join(
        _render_source_row(source)
        for source in sources
    ) or '<tr><td colspan="8">No source outcomes recorded.</td></tr>'

    banner = (
        f'<p class="admin-note" role="status">{_esc(preview_banner)}</p>'
        if preview_banner
        else ""
    )

    main = f"""<section class="admin-section" aria-labelledby="discovery-run-title">
      <p class="admin-breadcrumb"><a href="/admin/discovery">Discovery runs</a></p>
      <h1 class="admin-title" id="discovery-run-title">Run {_esc(str(run["id"])[:8])}…</h1>
      {banner}
      <dl class="admin-meta-grid">
        <div><dt>Trigger</dt><dd>{_esc(run.get("trigger_type"))}</dd></div>
        <div><dt>Status</dt><dd>{_esc(run.get("status"))}</dd></div>
        <div><dt>Actor</dt><dd>{_esc(run.get("actor") or "—")}</dd></div>
        <div><dt>Started</dt><dd>{_esc(run.get("started_at"))}</dd></div>
        <div><dt>Finished</dt><dd>{_esc(run.get("finished_at") or "—")}</dd></div>
        <div><dt>Sources</dt><dd>{_format_sources(run.get("enabled_sources"))}</dd></div>
        <div><dt>Lock</dt><dd>{_esc("acquired" if run.get("lock_acquired") else "not acquired")}</dd></div>
        <div><dt>Correlation</dt><dd><code>{_esc(run.get("correlation_id"))}</code></dd></div>
      </dl>
      {"<p class='form-error'>" + _esc(run.get("error_message")) + "</p>" if run.get("error_message") else ""}
      <p><a class="cta" href="/admin/discovery/inbox?run_id={_esc(run["id"])}">Review run candidates</a></p>
      <div class="admin-table-wrap">
        <table class="admin-table">
          <thead>
            <tr>
              <th>Source</th><th>Status</th><th>Fetched</th><th>Accepted</th>
              <th>Rejected</th><th>Errors</th><th>Checkpoint</th><th>Details</th>
            </tr>
          </thead>
          <tbody>{source_rows}</tbody>
        </table>
      </div>
    </section>"""
    return render_admin_shell(
        title="Discovery run",
        main=main,
        active_path="/admin/discovery",
        admin_username=admin_username,
        csrf_token=csrf_token,
    )


def _render_source_row(source: dict[str, Any]) -> str:
    checkpoint_parts = []
    if source.get("checkpoint_cursor"):
        checkpoint_parts.append(f"cursor={source['checkpoint_cursor']}")
    if source.get("checkpoint_etag"):
        checkpoint_parts.append(f"etag={source['checkpoint_etag']}")
    if source.get("checkpoint_last_modified"):
        checkpoint_parts.append(f"modified={source['checkpoint_last_modified']}")
    checkpoint = ", ".join(checkpoint_parts) or "—"
    errors = source.get("errors") or []
    if isinstance(errors, str):
        try:
            errors = json.loads(errors)
        except json.JSONDecodeError:
            errors = []
    error_summary = f"{len(errors)}"
    if errors:
        first = errors[0]
        if isinstance(first, dict) and first.get("message"):
            error_summary = _esc(first["message"])
    return f"""<tr>
      <td>{_esc(source.get("source_id"))}</td>
      <td>{_esc(source.get("status"))}</td>
      <td>{_esc(source.get("fetched_count", 0))}</td>
      <td>{_esc(source.get("accepted_count", 0))}</td>
      <td>{_esc(source.get("rejected_count", 0))}</td>
      <td>{_esc(source.get("error_count", 0))}</td>
      <td><code>{_esc(checkpoint)}</code></td>
      <td>{error_summary}</td>
    </tr>"""
