"""Admin HTML for ICP qualification scoring."""

from __future__ import annotations

import html
from datetime import datetime
from typing import Any

from app.admin_layout import render_admin_shell
from app.icp_scoring import ICP_DIMENSIONS


def _esc(value: Any) -> str:
    return "" if value is None else html.escape(str(value), quote=True)


def _format_score(value: Any) -> str:
    if value is None:
        return "—"
    return f"{float(value):.1f}"


def _format_timestamp(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M UTC")
    return _esc(value)


def _format_evidence_item(item: Any) -> str:
    if not isinstance(item, dict):
        return _esc(item)
    if item.get("label"):
        return _esc(item["label"])
    if item.get("observed_value"):
        return _esc(item["observed_value"])
    field = item.get("field")
    value = item.get("value")
    if field and value is not None:
        return _esc(f"{field}={value}")
    record_type = item.get("record_type")
    if record_type:
        return _esc(record_type)
    return _esc(item)


def _status_badge(status: str) -> str:
    modifier = {
        "scored": "icp-status--scored",
        "missing_data": "icp-status--missing",
        "expired_only": "icp-status--expired",
        "hypothesis_only": "icp-status--hypothesis",
        "disabled": "icp-status--disabled",
    }.get(status, "icp-status--missing")
    label = status.replace("_", " ")
    return f'<span class="icp-status {modifier}">{_esc(label)}</span>'


def render_icp_scores_list_page(
    *,
    rows: list[dict[str, Any]],
    active_version: dict[str, Any] | None,
    csrf_token: str,
    admin_username: str,
    preview_banner: str | None = None,
) -> str:
    banner_html = ""
    if preview_banner:
        banner_html = (
            f'<p class="admin-preview-banner" role="status">{_esc(preview_banner)}</p>'
        )
    version_label = (
        f"v{active_version['version_number']} — {_esc(active_version.get('label'))}"
        if active_version
        else "No active version"
    )
    table_rows = "".join(
        f"""<tr>
          <td><a href="/admin/signals/{_esc(row.get('company_id'))}">{_esc(row.get('company_name'))}</a></td>
          <td class="icp-score-cell{' icp-score-cell--override' if row.get('is_override') else ''}">{_format_score(row.get('total_score'))}</td>
          <td>{_esc(row.get('version_number'))}</td>
          <td>{'Override' if row.get('is_override') else 'Calculated'}</td>
          <td>{_format_timestamp(row.get('calculated_at'))}</td>
        </tr>"""
        for row in rows
    ) or '<tr><td colspan="5">No ICP scores yet. Recalculate from a company detail page.</td></tr>'
    main = f"""<section class="admin-section" aria-labelledby="icp-scores-title">
      {banner_html}
      <div class="admin-section-head">
        <div>
          <p class="admin-eyebrow">Signal intelligence</p>
          <h1 class="admin-title" id="icp-scores-title">ICP scores</h1>
          <p class="admin-lede">Deterministic qualification scoring with inspectable rule contributions.</p>
          <p class="admin-note">Active rules: <strong>{version_label}</strong></p>
        </div>
        <div class="admin-section-actions">
          <a class="cta admin-submit" href="/admin/signals/rules">Edit rules</a>
        </div>
      </div>
      <div class="admin-table-wrap">
        <table class="admin-table">
          <thead><tr><th>Company</th><th>Score</th><th>Version</th><th>Type</th><th>Calculated</th></tr></thead>
          <tbody>{table_rows}</tbody>
        </table>
      </div>
    </section>"""
    return render_admin_shell(
        title="ICP scores",
        main=main,
        active_path="/admin/signals",
        admin_username=admin_username,
        csrf_token=csrf_token,
    )


def _dimension_options(selected: str) -> str:
    return "".join(
        f'<option value="{_esc(key)}"{" selected" if key == selected else ""}>'
        f'{_esc(key.replace("_", " "))}</option>'
        for key in sorted(ICP_DIMENSIONS)
    )


def render_icp_rules_page(
    *,
    rules: list[dict[str, Any]],
    active_version: dict[str, Any] | None,
    csrf_token: str,
    admin_username: str,
    preview_banner: str | None = None,
    error_message: str | None = None,
) -> str:
    banner_html = ""
    if preview_banner:
        banner_html = (
            f'<p class="admin-preview-banner" role="status">{_esc(preview_banner)}</p>'
        )
    error_html = ""
    if error_message:
        error_html = (
            f'<p class="admin-field-error" role="alert">{_esc(error_message)}</p>'
        )
    version_label = (
        f"v{active_version['version_number']} — {_esc(active_version.get('label'))}"
        if active_version
        else "No active version"
    )
    rule_rows = []
    for rule in rules:
        rule_id = _esc(rule["id"])
        threshold = rule.get("threshold") or {}
        keywords = ", ".join(threshold.get("keywords") or [])
        max_days = threshold.get("max_days")
        rule_rows.append(
            f"""<tr>
              <td><code>{rule_id}</code></td>
              <td><input name="label__{rule_id}" value="{_esc(rule.get('label'))}" /></td>
              <td><select name="dimension__{rule_id}">{_dimension_options(str(rule.get('dimension')))}</select></td>
              <td><input name="weight__{rule_id}" type="number" min="0" max="10" step="0.1" value="{_esc(rule.get('weight'))}" /></td>
              <td><input name="max_days__{rule_id}" type="number" min="0" step="1" value="{_esc(max_days if max_days is not None else '')}" placeholder="—" /></td>
              <td><input name="keywords__{rule_id}" value="{_esc(keywords)}" /></td>
              <td><label><input type="checkbox" name="enabled__{rule_id}" value="1"{' checked' if rule.get('enabled', True) else ''} /> On</label></td>
              <td><label><input type="checkbox" name="accept_hypothesis__{rule_id}" value="1"{' checked' if rule.get('accept_hypothesis') else ''} /> Hypothesis</label></td>
            </tr>"""
        )
    main = f"""<section class="admin-section" aria-labelledby="icp-rules-title">
      {banner_html}
      <div class="admin-section-head">
        <div>
          <p class="admin-eyebrow">Signal intelligence</p>
          <h1 class="admin-title" id="icp-rules-title">ICP scoring rules</h1>
          <p class="admin-lede">Edit weights and thresholds. Saving creates a new audited version without rewriting historical snapshots.</p>
          <p class="admin-note">Active version: <strong>{version_label}</strong></p>
        </div>
        <div class="admin-section-actions">
          <a class="admin-exit" href="/admin/signals">Back to scores</a>
        </div>
      </div>
      {error_html}
      <form class="admin-form" method="post" action="/admin/signals/rules">
        <input type="hidden" name="csrf_token" value="{_esc(csrf_token)}" />
        <div class="admin-table-wrap">
          <table class="admin-table">
            <thead><tr><th>Rule</th><th>Label</th><th>Dimension</th><th>Weight</th><th>Max days</th><th>Keywords</th><th>Enabled</th><th>Hypothesis</th></tr></thead>
            <tbody>{''.join(rule_rows)}</tbody>
          </table>
        </div>
        <button class="cta admin-submit" type="submit">Publish new rule version</button>
      </form>
    </section>"""
    return render_admin_shell(
        title="ICP rules",
        main=main,
        active_path="/admin/signals",
        admin_username=admin_username,
        csrf_token=csrf_token,
    )


def render_icp_score_detail_page(
    *,
    company: dict[str, Any],
    snapshot: dict[str, Any] | None,
    active_version: dict[str, Any] | None,
    csrf_token: str,
    admin_username: str,
    preview_banner: str | None = None,
    error_message: str | None = None,
) -> str:
    banner_html = ""
    if preview_banner:
        banner_html = (
            f'<p class="admin-preview-banner" role="status">{_esc(preview_banner)}</p>'
        )
    error_html = ""
    if error_message:
        error_html = (
            f'<p class="admin-field-error" role="alert">{_esc(error_message)}</p>'
        )

    score_class = "icp-score-display"
    score_label = "Total score"
    total_score = None
    computed_score = None
    calculated_at = None
    version_number = None
    missing_inputs: list[str] = []
    breakdown_rows = '<tr><td colspan="5">No score calculated yet.</td></tr>'

    if snapshot:
        total_score = snapshot.get("total_score")
        computed_score = snapshot.get("computed_score")
        calculated_at = snapshot.get("calculated_at")
        version_number = snapshot.get("version_number")
        missing_inputs = snapshot.get("missing_inputs") or []
        if snapshot.get("is_override"):
            score_class += " icp-score-display--override"
            score_label = "Override score"
        breakdown = snapshot.get("breakdown") or []
        breakdown_rows = "".join(
            f"""<tr>
              <td><code>{_esc(item.get('rule_id'))}</code></td>
              <td>{_esc(item.get('label'))}</td>
              <td>{_format_score(item.get('points_awarded'))} / {_format_score(item.get('weight'))}</td>
              <td>{_status_badge(str(item.get('status', 'missing_data')))}</td>
              <td>{_esc('; '.join(item.get('missing_inputs') or []) or '—')}</td>
            </tr>"""
            for item in breakdown
        ) or breakdown_rows

    evidence_html = ""
    if snapshot:
        for item in snapshot.get("breakdown") or []:
            evidence_items = item.get("evidence") or []
            if not evidence_items:
                continue
            formatted = "; ".join(_format_evidence_item(entry) for entry in evidence_items)
            evidence_html += (
                f"<li><strong>{_esc(item.get('rule_id'))}</strong>: {formatted}</li>"
            )
    evidence_block = ""
    if evidence_html:
        evidence_block = f"<ul class=\"icp-evidence-list\">{evidence_html}</ul>"

    missing_block = ""
    if missing_inputs:
        missing_block = (
            "<p class=\"admin-note\">Missing inputs: "
            + ", ".join(_esc(item) for item in missing_inputs)
            + "</p>"
        )

    version_label = (
        f"v{active_version['version_number']}" if active_version else "—"
    )
    override_fields = ""
    if snapshot and snapshot.get("is_override"):
        override_fields = (
            f'<p class="admin-note icp-override-note">Manual override by '
            f'<strong>{_esc(snapshot.get("override_by"))}</strong>: '
            f'{_esc(snapshot.get("override_reason"))}. Computed score was '
            f'{_format_score(computed_score)}.</p>'
        )

    main = f"""<section class="admin-section" aria-labelledby="icp-detail-title">
      {banner_html}
      <div class="admin-section-head">
        <div>
          <p class="admin-eyebrow">Signal intelligence</p>
          <h1 class="admin-title" id="icp-detail-title">{_esc(company.get('name'))}</h1>
          <p class="admin-lede">Inspectable ICP qualification breakdown for this company.</p>
          <p class="admin-note"><a href="/admin/companies/{_esc(company.get('id'))}">View company</a> · <a href="/admin/signals">All scores</a></p>
        </div>
      </div>
      {error_html}
      <div class="icp-score-summary">
        <div class="{score_class}">
          <p class="admin-eyebrow">{score_label}</p>
          <p class="icp-score-value">{_format_score(total_score)}</p>
          <p class="admin-note">Version {_esc(version_number or version_label)} · {_format_timestamp(calculated_at)}</p>
        </div>
        {override_fields}
        {missing_block}
      </div>
      <form class="admin-form admin-form--compact" method="post" action="/admin/signals/{_esc(company.get('id'))}/recalculate">
        <input type="hidden" name="csrf_token" value="{_esc(csrf_token)}" />
        <button class="cta admin-submit" type="submit">Recalculate score</button>
      </form>
      <div class="admin-table-wrap">
        <table class="admin-table">
          <thead><tr><th>Rule</th><th>Label</th><th>Points</th><th>Status</th><th>Missing</th></tr></thead>
          <tbody>{breakdown_rows}</tbody>
        </table>
      </div>
      {evidence_block}
      <form class="admin-form" method="post" action="/admin/signals/{_esc(company.get('id'))}/override">
        <input type="hidden" name="csrf_token" value="{_esc(csrf_token)}" />
        <h2 class="admin-subtitle">Manual override</h2>
        <p class="admin-note">Overrides require a reason and remain visually distinct from calculated scores.</p>
        <div class="field">
          <label for="override-score">Override score (0–10)</label>
          <input id="override-score" name="override_score" type="number" min="0" max="10" step="0.1" required />
        </div>
        <div class="field">
          <label for="override-reason">Reason</label>
          <textarea id="override-reason" name="reason" rows="3" required></textarea>
        </div>
        <button class="admin-action admin-action--secondary" type="submit">Apply override</button>
      </form>
    </section>"""
    return render_admin_shell(
        title=f"ICP score · {company.get('name', 'Company')}",
        main=main,
        active_path="/admin/signals",
        admin_username=admin_username,
        csrf_token=csrf_token,
    )
