"""Admin HTML for acquisition pipeline management."""

from __future__ import annotations

import html
from datetime import datetime
from typing import Any

from app.acquisition_pipeline import PIPELINE_ACTIVITY_TYPES
from app.pipeline_stages import PIPELINE_STAGES, pipeline_stage_label
from app.admin_layout import render_admin_shell


def _esc(value: Any) -> str:
    return "" if value is None else html.escape(str(value), quote=True)


def _format_due(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M UTC")
    return _esc(value)


def _format_value_cents(cents: Any) -> str:
    if cents is None:
        return "—"
    return f"${int(cents) // 100:,}"


def _stage_options(selected: str | None) -> str:
    rows = [f'<option value="">{html.escape("Any stage")}</option>']
    rows.extend(
        f'<option value="{_esc(key)}"{" selected" if key == selected else ""}>{_esc(label)}</option>'
        for key, label in PIPELINE_STAGES.items()
    )
    return "\n".join(rows)


def _activity_type_options() -> str:
    return "\n".join(
        f'<option value="{_esc(key)}">{_esc(label)}</option>'
        for key, label in PIPELINE_ACTIVITY_TYPES.items()
    )


def render_pipeline_list_page(
    *,
    companies: list[dict[str, Any]],
    stage_filter: str | None,
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
          <td><a href="/admin/pipeline/{_esc(row["id"])}">{_esc(row.get("name"))}</a></td>
          <td>{_esc(pipeline_stage_label(row.get("pipeline_stage")))}</td>
          <td>{_format_value_cents(row.get("expected_value_cents"))}</td>
          <td>{_esc(row.get("next_action") or "—")}</td>
          <td>{_format_due(row.get("next_action_due_at"))}</td>
          <td>{_esc(row.get("pipeline_owner") or "—")}</td>
        </tr>"""
        for row in companies
    ) or '<tr><td colspan="6">No companies in the pipeline yet.</td></tr>'
    main = f"""<section class="admin-section" aria-labelledby="pipeline-title">
      {banner_html}
      <div class="admin-section-head">
        <div>
          <p class="admin-eyebrow">CRM</p>
          <h1 class="admin-title" id="pipeline-title">Pipeline</h1>
          <p class="admin-lede">Move qualified companies from research through paid engagements.</p>
        </div>
      </div>
      <form class="admin-form admin-form--compact" method="get" action="/admin/pipeline">
        <div class="field"><label for="stage-filter">Stage</label><select id="stage-filter" name="stage">{_stage_options(stage_filter)}</select></div>
        <button class="cta admin-submit" type="submit">Filter</button>
      </form>
      <div class="admin-table-wrap">
        <table class="admin-table">
          <thead><tr><th>Company</th><th>Stage</th><th>Value</th><th>Next action</th><th>Due</th><th>Owner</th></tr></thead>
          <tbody>{rows}</tbody>
        </table>
      </div>
    </section>"""
    return render_admin_shell(
        title="Pipeline",
        main=main,
        active_path="/admin/pipeline",
        admin_username=admin_username,
        csrf_token=csrf_token,
    )


def render_pipeline_detail_page(
    *,
    company: dict[str, Any],
    history: list[dict[str, Any]],
    activities: list[dict[str, Any]],
    csrf_token: str,
    admin_username: str,
    error_message: str | None = None,
    preview_banner: str | None = None,
) -> str:
    banner_html = ""
    if preview_banner:
        banner_html = (
            f'<p class="admin-preview-banner" role="status">{_esc(preview_banner)}</p>'
        )
    stage = company.get("pipeline_stage")
    history_rows = "".join(
        f"""<tr>
          <td>{_format_due(row.get("changed_at"))}</td>
          <td>{_esc(pipeline_stage_label(row.get("from_stage")))}</td>
          <td>{_esc(pipeline_stage_label(row.get("to_stage")))}</td>
          <td>{_esc(row.get("changed_by"))}</td>
        </tr>"""
        for row in history
    ) or '<tr><td colspan="4">No stage changes yet.</td></tr>'
    activity_rows = "".join(
        f"""<tr>
          <td>{_format_due(row.get("created_at"))}</td>
          <td>{_esc(PIPELINE_ACTIVITY_TYPES.get(str(row.get("activity_type")), row.get("activity_type")))}</td>
          <td>{_esc(row.get("summary"))}</td>
        </tr>"""
        for row in activities
    ) or '<tr><td colspan="3">No activities yet.</td></tr>'
    stage_transition_options = "\n".join(
        f'<option value="{_esc(key)}">{_esc(label)}</option>'
        for key, label in PIPELINE_STAGES.items()
        if key != stage
    )
    due_value = ""
    raw_due = company.get("next_action_due_at")
    if isinstance(raw_due, datetime):
        due_value = raw_due.strftime("%Y-%m-%dT%H:%M")
    main = f"""<section class="admin-section" aria-labelledby="pipeline-detail-title">
      {banner_html}
      <p class="admin-breadcrumb"><a href="/admin/pipeline">Pipeline</a></p>
      <h1 class="admin-title" id="pipeline-detail-title">{_esc(company.get("name"))}</h1>
      <p class="admin-lede">Stage: <strong>{_esc(pipeline_stage_label(stage))}</strong></p>
      {'<p class="form-error" role="alert">' + _esc(error_message) + '</p>' if error_message else ''}
      <div class="dashboard-panel">
        <h2 class="admin-section-title">Next action</h2>
        <form class="admin-form admin-form--editor" method="post" action="/admin/pipeline/{_esc(company["id"])}/next-action">
          <input type="hidden" name="csrf_token" value="{_esc(csrf_token)}" />
          <div class="field"><label for="next_action">Action</label><textarea id="next_action" name="next_action" rows="3" maxlength="2000">{_esc(company.get("next_action"))}</textarea></div>
          <div class="field"><label for="next_action_due_at">Due</label><input id="next_action_due_at" name="next_action_due_at" type="datetime-local" value="{_esc(due_value)}" /></div>
          <div class="field"><label for="pipeline_owner">Owner</label><input id="pipeline_owner" name="pipeline_owner" maxlength="200" value="{_esc(company.get("pipeline_owner"))}" /></div>
          <div class="field"><label for="expected_value_cents">Expected value (cents)</label><input id="expected_value_cents" name="expected_value_cents" type="number" min="0" value="{_esc(company.get("expected_value_cents"))}" /></div>
          <button class="cta admin-submit" type="submit">Save next action</button>
        </form>
      </div>
      <div class="dashboard-panel">
        <h2 class="admin-section-title">Change stage</h2>
        <form class="admin-form admin-form--editor" method="post" action="/admin/pipeline/{_esc(company["id"])}/stage">
          <input type="hidden" name="csrf_token" value="{_esc(csrf_token)}" />
          <div class="field"><label for="to_stage">New stage</label><select id="to_stage" name="to_stage" required>{stage_transition_options}</select></div>
          <div class="field"><label for="loss_reason">Loss reason</label><input id="loss_reason" name="loss_reason" maxlength="2000" placeholder="Required when moving to Lost" /></div>
          <div class="field"><label for="nurture_reason">Nurture reason</label><input id="nurture_reason" name="nurture_reason" maxlength="2000" placeholder="Required when moving to Nurture" /></div>
          <div class="field"><label><input type="checkbox" name="confirm" value="1" /> Confirm non-standard transition</label></div>
          <button class="cta admin-submit" type="submit">Update stage</button>
        </form>
      </div>
      <div class="dashboard-panel">
        <h2 class="admin-section-title">Log activity</h2>
        <form class="admin-form admin-form--editor" method="post" action="/admin/pipeline/{_esc(company["id"])}/activities">
          <input type="hidden" name="csrf_token" value="{_esc(csrf_token)}" />
          <div class="field"><label for="activity_type">Type</label><select id="activity_type" name="activity_type" required>{_activity_type_options()}</select></div>
          <div class="field"><label for="summary">Summary</label><textarea id="summary" name="summary" rows="3" required maxlength="5000"></textarea></div>
          <button class="cta admin-submit" type="submit">Add activity</button>
        </form>
      </div>
      <div class="dashboard-panel">
        <h2 class="admin-section-title">Stage history</h2>
        <div class="admin-table-wrap"><table class="admin-table"><thead><tr><th>When</th><th>From</th><th>To</th><th>By</th></tr></thead><tbody>{history_rows}</tbody></table></div>
      </div>
      <div class="dashboard-panel">
        <h2 class="admin-section-title">Activities</h2>
        <div class="admin-table-wrap"><table class="admin-table"><thead><tr><th>When</th><th>Type</th><th>Summary</th></tr></thead><tbody>{activity_rows}</tbody></table></div>
      </div>
    </section>"""
    return render_admin_shell(
        title=f"Pipeline — {company.get('name', '')}",
        main=main,
        active_path="/admin/pipeline",
        admin_username=admin_username,
        csrf_token=csrf_token,
    )
