"""HTML for the acquisition admin dashboard."""

from __future__ import annotations

import html
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.acquisition_dashboard import (
    AcquisitionDashboardData,
    CountBucket,
    EvidenceRow,
    NextActionRow,
    UPCOMING_ACTION_WINDOW_DAYS,
    dashboard_is_empty,
)
from app.acquisition_pipeline import pipeline_stage_label
from app.admin_layout import render_admin_shell
from app.companies import COMPANY_CATEGORIES, COMPANY_STAGES, TARGET_STATUSES
from app.research_records import RECORD_TYPE_LABELS


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _format_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo("UTC"))
    return _esc(value.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M UTC"))


def _render_count_table(
    *,
    title: str,
    buckets: tuple[CountBucket, ...],
    definition: str,
    section_id: str,
) -> str:
    if buckets:
        rows = "".join(
            f"<tr><td>{_esc(bucket.label)}</td><td>{bucket.count}</td></tr>"
            for bucket in buckets
        )
        table_body = rows
    else:
        table_body = '<tr><td colspan="2" class="audit-empty">No records yet.</td></tr>'
    return f"""<section class="dashboard-panel" aria-labelledby="{section_id}">
      <h2 class="admin-section-title" id="{section_id}">{_esc(title)}</h2>
      <p class="dashboard-metric-def">{_esc(definition)}</p>
      <div class="admin-table-wrap">
        <table class="admin-table dashboard-count-table">
          <thead><tr><th scope="col">Bucket</th><th scope="col">Count</th></tr></thead>
          <tbody>{table_body}</tbody>
        </table>
      </div>
    </section>"""


def _render_next_action_rows(rows: tuple[NextActionRow, ...]) -> str:
    if not rows:
        return '<tr><td colspan="4" class="audit-empty">Nothing scheduled.</td></tr>'
    return "".join(
        f"""<tr>
          <td><a href="/admin/pipeline/{_esc(row.company_id)}">{_esc(row.company_name)}</a></td>
          <td>{_esc(row.pipeline_owner or "—")}</td>
          <td>{_esc(row.next_action[:120] + ("…" if len(row.next_action) > 120 else ""))}</td>
          <td><time datetime="{_esc(row.next_action_due_at.isoformat())}">{_format_timestamp(row.next_action_due_at)}</time></td>
        </tr>"""
        for row in rows
    )


def _render_evidence_rows(rows: tuple[EvidenceRow, ...], *, stale: bool) -> str:
    if not rows:
        empty = "No stale evidence." if stale else "No evidence recorded yet."
        return f'<tr><td colspan="4" class="audit-empty">{empty}</td></tr>'
    body = []
    for row in rows:
        type_label = RECORD_TYPE_LABELS.get(row.record_type, row.record_type)
        when = row.expires_at if stale else row.created_at
        when_label = "Expired" if stale else "Added"
        body.append(
            f"""<tr>
              <td><a href="/admin/companies/{_esc(row.company_id)}">{_esc(row.company_name)}</a></td>
              <td>{_esc(type_label)}</td>
              <td>{_esc(row.body[:100] + ("…" if len(row.body) > 100 else ""))}</td>
              <td><time datetime="{_esc(when.isoformat())}">{when_label}: {_format_timestamp(when)}</time></td>
            </tr>"""
        )
    return "".join(body)


def _company_label(row: Any) -> str:
    category = COMPANY_CATEGORIES.get(str(row.category or ""), row.category or "—")
    stage = COMPANY_STAGES.get(str(row.stage or ""), row.stage or "—")
    target = TARGET_STATUSES.get(str(row.target_status or ""), row.target_status or "—")
    return f"{category} · {stage} · {target}"


def _render_attention_rows(
    rows: tuple[Any, ...],
    *,
    empty_message: str,
    use_pipeline_stage: bool = False,
) -> str:
    if not rows:
        return f'<tr><td colspan="2" class="audit-empty">{_esc(empty_message)}</td></tr>'
    return "".join(
        f"""<tr>
          <td><a href="/admin/pipeline/{_esc(row.company_id)}">{_esc(row.company_name)}</a></td>
          <td>{_esc(_attention_label(row, use_pipeline_stage=use_pipeline_stage))}</td>
        </tr>"""
        for row in rows
    )


def _attention_label(row: Any, *, use_pipeline_stage: bool) -> str:
    if use_pipeline_stage and getattr(row, "pipeline_stage", None):
        return pipeline_stage_label(row.pipeline_stage)
    return _company_label(row)


def render_acquisition_dashboard_page(
    *,
    data: AcquisitionDashboardData,
    admin_username: str,
    csrf_token: str = "",
    db_error: bool = False,
    preview_banner: str | None = None,
) -> str:
    generated = _format_timestamp(data.generated_at)
    definitions = data.metric_definitions
    banner_html = ""
    if preview_banner:
        banner_html = (
            f'<p class="admin-preview-banner" role="status">{_esc(preview_banner)}</p>'
        )

    if db_error:
        empty_block = """<p class="brief-error" role="alert">
            Dashboard metrics are temporarily unavailable. Try again shortly.
          </p>"""
    elif dashboard_is_empty(data):
        empty_block = """<section class="dashboard-empty" aria-labelledby="dashboard-empty-title">
          <h2 class="admin-section-title" id="dashboard-empty-title">Start building your pipeline</h2>
          <p class="admin-lede">
            No CRM records yet. Add companies and contacts manually today — bulk import and
            discovery lists ship in later milestones.
          </p>
          <p class="dashboard-actions">
            <a class="cta" href="/admin/companies/new">Add company</a>
            <a class="dashboard-secondary-link" href="/admin/companies">Browse companies</a>
          </p>
          <p class="admin-note">
            Future: CSV import (<code>/admin/imports</code>) and prospect discovery
            (<code>/admin/discovery</code>) will populate these sections automatically.
          </p>
        </section>"""
    else:
        empty_block = ""

    main = f"""<section class="admin-panel dashboard-root" aria-labelledby="dashboard-title">
      {banner_html}
      <p class="admin-eyebrow">Acquisition</p>
      <h1 class="admin-title" id="dashboard-title">Today&apos;s attention</h1>
      <p class="admin-lede">
        Operational snapshot for targets requiring follow-up, evidence review, or contact coverage.
        Generated <time datetime="{generated}">{generated}</time>.
      </p>
      {empty_block}
      <div class="dashboard-grid">
        {_render_count_table(title="Companies by funding stage", buckets=data.company_counts_by_stage, definition=definitions["company_count_by_stage"], section_id="dash-companies-stage")}
        {_render_count_table(title="Companies by category", buckets=data.company_counts_by_category, definition=definitions["company_count_by_category"], section_id="dash-companies-category")}
        {_render_count_table(title="Contacts by company funding stage", buckets=data.contact_counts_by_stage, definition=definitions["contact_count_by_stage"], section_id="dash-contacts-stage")}
        {_render_count_table(title="Contacts by company category", buckets=data.contact_counts_by_category, definition=definitions["contact_count_by_category"], section_id="dash-contacts-category")}
      </div>
      <section class="dashboard-panel" aria-labelledby="dash-overdue-title">
        <h2 class="admin-section-title" id="dash-overdue-title">Overdue next actions</h2>
        <p class="dashboard-metric-def">{_esc(definitions["overdue_next_action"])}</p>
        <div class="admin-table-wrap">
          <table class="admin-table">
            <thead><tr><th>Company</th><th>Owner</th><th>Next action</th><th>Due</th></tr></thead>
            <tbody>{_render_next_action_rows(data.overdue_actions)}</tbody>
          </table>
        </div>
      </section>
      <section class="dashboard-panel" aria-labelledby="dash-upcoming-title">
        <h2 class="admin-section-title" id="dash-upcoming-title">Upcoming next actions ({UPCOMING_ACTION_WINDOW_DAYS}d)</h2>
        <p class="dashboard-metric-def">{_esc(definitions["upcoming_next_action"])}</p>
        <div class="admin-table-wrap">
          <table class="admin-table">
            <thead><tr><th>Company</th><th>Owner</th><th>Next action</th><th>Due</th></tr></thead>
            <tbody>{_render_next_action_rows(data.upcoming_actions)}</tbody>
          </table>
        </div>
      </section>
      <section class="dashboard-panel" aria-labelledby="dash-recent-evidence-title">
        <h2 class="admin-section-title" id="dash-recent-evidence-title">Recently added evidence</h2>
        <p class="dashboard-metric-def">{_esc(definitions["recent_evidence"])}</p>
        <div class="admin-table-wrap">
          <table class="admin-table">
            <thead><tr><th>Company</th><th>Type</th><th>Summary</th><th>When</th></tr></thead>
            <tbody>{_render_evidence_rows(data.recent_evidence, stale=False)}</tbody>
          </table>
        </div>
      </section>
      <section class="dashboard-panel" aria-labelledby="dash-stale-evidence-title">
        <h2 class="admin-section-title" id="dash-stale-evidence-title">Stale evidence</h2>
        <p class="dashboard-metric-def">{_esc(definitions["stale_evidence"])}</p>
        <div class="admin-table-wrap">
          <table class="admin-table">
            <thead><tr><th>Company</th><th>Type</th><th>Summary</th><th>When</th></tr></thead>
            <tbody>{_render_evidence_rows(data.stale_evidence, stale=True)}</tbody>
          </table>
        </div>
      </section>
      <section class="dashboard-panel" aria-labelledby="dash-no-dm-title">
        <h2 class="admin-section-title" id="dash-no-dm-title">Missing decision-maker</h2>
        <p class="dashboard-metric-def">{_esc(definitions["without_decision_maker"])}</p>
        <div class="admin-table-wrap">
          <table class="admin-table">
            <thead><tr><th>Company</th><th>Profile</th></tr></thead>
            <tbody>{_render_attention_rows(data.without_decision_maker, empty_message="All targets have at least one contact.")}</tbody>
          </table>
        </div>
        <p class="admin-note dashboard-footnote">
          <a href="/admin/companies/new">Add a company</a> or attach contacts from the company record.
        </p>
      </section>
      <section class="dashboard-panel" aria-labelledby="dash-no-action-title">
        <h2 class="admin-section-title" id="dash-no-action-title">Missing next action</h2>
        <p class="dashboard-metric-def">{_esc(definitions["without_next_action"])}</p>
        <div class="admin-table-wrap">
          <table class="admin-table">
            <thead><tr><th>Company</th><th>Profile</th></tr></thead>
            <tbody>{_render_attention_rows(data.without_next_action, empty_message="Every pipeline company has a scheduled next action.", use_pipeline_stage=True)}</tbody>
          </table>
        </div>
        <p class="admin-note dashboard-footnote">
          Set the canonical next action from the company&apos;s
          <a href="/admin/pipeline">pipeline detail</a> page.
        </p>
      </section>
    </section>"""
    return render_admin_shell(
        title="Dashboard",
        main=main,
        active_path="/admin",
        admin_username=admin_username,
        csrf_token=csrf_token,
    )
