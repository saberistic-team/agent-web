"""Admin HTML for tier A/B/C qualification target lists."""

from __future__ import annotations

import html
from datetime import datetime
from typing import Any

from app.admin_layout import render_admin_shell
from app.companies import COMPANY_CATEGORIES, COMPANY_STAGES, FRESHNESS_FILTERS
from app.pipeline_stages import PIPELINE_STAGES, pipeline_stage_label
from app.qualification_targets import (
    QUALIFICATION_TIERS,
    WARM_PATH_FILTERS,
    MAX_WORKING_LIST_ITEMS,
)


def _esc(value: Any) -> str:
    return "" if value is None else html.escape(str(value), quote=True)


def _options(registry: dict[str, str], selected: str | None, *, empty: str = "Any") -> str:
    rows = [f'<option value="">{html.escape(empty)}</option>']
    rows.extend(
        f'<option value="{_esc(key)}"{" selected" if key == selected else ""}>{_esc(label)}</option>'
        for key, label in registry.items()
    )
    return "\n".join(rows)


def _tier_options(selected: str | None) -> str:
    labels = {tier: f"Tier {tier} ({low}–{high})" for tier, (low, high) in QUALIFICATION_TIERS.items()}
    return _options(labels, selected)


def _format_signals(signals: list[str] | None) -> str:
    if not signals:
        return "—"
    return ", ".join(_esc(item) for item in signals)


def _format_missing(fields: list[str] | None) -> str:
    if not fields:
        return "—"
    return ", ".join(_esc(item) for item in fields)


def _format_freshness(value: str | None, *, stale_evidence: bool = False) -> str:
    if not value:
        return "—"
    label = FRESHNESS_FILTERS.get(value, value.replace("_", " ").title())
    if stale_evidence and value != "stale":
        return f"{_esc(label)} (stale evidence)"
    return _esc(label)


def _format_timestamp(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M UTC")
    return _esc(value)


def render_targets_list_page(
    *,
    targets: list[dict[str, Any]],
    filters: dict[str, str | None],
    working_lists: list[dict[str, Any]],
    csrf_token: str,
    admin_username: str,
    preview_banner: str | None = None,
    save_message: str | None = None,
    save_error: str | None = None,
) -> str:
    banner_html = ""
    if preview_banner:
        banner_html = (
            f'<p class="admin-preview-banner" role="status">{_esc(preview_banner)}</p>'
        )
    status_html = ""
    if save_message:
        status_html = f'<p class="admin-status" role="status">{_esc(save_message)}</p>'
    elif save_error:
        status_html = f'<p class="form-error" role="alert">{_esc(save_error)}</p>'

    rows = "".join(
        f"""<tr>
          <td><input type="checkbox" name="company_ids" value="{_esc(row.get("company_id") or row.get("id"))}" aria-label="Select {_esc(row.get("name"))}" /></td>
          <td><span class="admin-tier admin-tier--{ _esc(row.get("tier", "").lower()) }">{_esc(row.get("tier"))}</span></td>
          <td><a href="/admin/targets/{_esc(row.get("company_id") or row.get("id"))}">{_esc(row.get("name"))}</a></td>
          <td>{_esc(row.get("score"))}</td>
          <td>{_esc(COMPANY_STAGES.get(str(row.get("stage")), row.get("stage") or "—"))}</td>
          <td>{_esc(COMPANY_CATEGORIES.get(str(row.get("vertical")), row.get("vertical") or "—"))}</td>
          <td>{_format_signals(row.get("strongest_signals"))}</td>
          <td>{_esc(row.get("warm_path") or "—")}</td>
          <td>{_esc(row.get("next_action") or "—")}</td>
          <td>{_format_freshness(row.get("evidence_freshness"), stale_evidence=bool(row.get("stale_evidence")))}</td>
          <td>{_format_missing(row.get("missing_fields"))}</td>
          <td>{_esc(pipeline_stage_label(row.get("pipeline_stage")))}</td>
          <td>{_esc(row.get("pipeline_owner") or "—")}</td>
        </tr>"""
        for row in targets
    ) or '<tr><td colspan="13">No active targets match these filters. Scores below 4 are excluded.</td></tr>'

    working_rows = "".join(
        f"""<tr>
          <td>{_esc(item.get("name"))}</td>
          <td>{_esc(item.get("item_count", 0))}</td>
          <td>{_format_timestamp(item.get("updated_at"))}</td>
        </tr>"""
        for item in working_lists
    ) or '<tr><td colspan="3">No saved working lists yet.</td></tr>'

    main = f"""<section class="admin-section" aria-labelledby="targets-title">
      {banner_html}
      {status_html}
      <div class="admin-section-head">
        <div>
          <p class="admin-eyebrow">Qualification</p>
          <h1 class="admin-title" id="targets-title">Target lists</h1>
          <p class="admin-lede">Tier A (8–10), B (6–7), and C (4–5) from deterministic ICP scores. Sorted by score, then tier, then name.</p>
        </div>
      </div>
      <form class="admin-form admin-form--compact" method="get" action="/admin/targets">
        <div class="field"><label for="tier-filter">Tier</label><select id="tier-filter" name="tier">{_tier_options(filters.get("tier"))}</select></div>
        <div class="field"><label for="category-filter">Category</label><select id="category-filter" name="category">{_options(COMPANY_CATEGORIES, filters.get("category"))}</select></div>
        <div class="field"><label for="stage-filter">Stage</label><select id="stage-filter" name="stage">{_options(COMPANY_STAGES, filters.get("stage"))}</select></div>
        <div class="field"><label for="pipeline-filter">Pipeline state</label><select id="pipeline-filter" name="pipeline_stage">{_options(PIPELINE_STAGES, filters.get("pipeline_stage"), empty="Any pipeline stage")}</select></div>
        <div class="field"><label for="owner-filter">Owner</label><input id="owner-filter" name="owner" value="{_esc(filters.get("owner"))}" placeholder="Pipeline owner" /></div>
        <div class="field"><label for="freshness-filter">Freshness</label><select id="freshness-filter" name="freshness">{_options(FRESHNESS_FILTERS, filters.get("freshness"))}</select></div>
        <div class="field"><label for="warm-path-filter">Warm path</label><select id="warm-path-filter" name="warm_path">{_options(WARM_PATH_FILTERS, filters.get("warm_path"))}</select></div>
        <button class="cta admin-submit" type="submit">Filter</button>
      </form>
      <form class="admin-form" method="post" action="/admin/targets/working-list">
        <input type="hidden" name="csrf_token" value="{_esc(csrf_token)}" />
        <div class="admin-table-wrap">
          <table class="admin-table">
            <thead><tr>
              <th scope="col">Save</th>
              <th scope="col">Tier</th><th scope="col">Company</th><th scope="col">Score</th>
              <th scope="col">Stage</th><th scope="col">Vertical</th><th scope="col">Strongest signals</th>
              <th scope="col">Warm path</th><th scope="col">Next action</th><th scope="col">Evidence freshness</th>
              <th scope="col">Missing fields</th><th scope="col">Pipeline</th><th scope="col">Owner</th>
            </tr></thead>
            <tbody>{rows}</tbody>
          </table>
        </div>
        <p class="admin-hint">Tie-breakers: score (desc), tier (A→C), company name, then ID.</p>
        <div class="admin-form-row">
          <div class="field"><label for="list-name">Working list name</label><input id="list-name" name="name" required maxlength="200" placeholder="Q3 outreach shortlist" /></div>
          <button class="cta admin-submit" type="submit">Save working list (max {MAX_WORKING_LIST_ITEMS} IDs)</button>
        </div>
      </form>
      <section class="admin-subsection" aria-labelledby="working-lists-title">
        <h2 class="admin-subtitle" id="working-lists-title">Saved working lists</h2>
        <p class="admin-hint">Lists store company IDs only — no copied firmographics.</p>
        <div class="admin-table-wrap">
          <table class="admin-table">
            <thead><tr><th>Name</th><th>Companies</th><th>Updated</th></tr></thead>
            <tbody>{working_rows}</tbody>
          </table>
        </div>
      </section>
    </section>"""
    return render_admin_shell(
        title="Target lists",
        main=main,
        active_path="/admin/targets",
        admin_username=admin_username,
        csrf_token=csrf_token,
    )


def render_target_detail_page(
    *,
    company: dict[str, Any],
    target: dict[str, Any] | None,
    tier_history: list[dict[str, Any]],
    csrf_token: str,
    admin_username: str,
    preview_banner: str | None = None,
) -> str:
    banner_html = ""
    if preview_banner:
        banner_html = (
            f'<p class="admin-preview-banner" role="status">{_esc(preview_banner)}</p>'
        )
    history_rows = "".join(
        f"""<tr>
          <td>{_format_timestamp(item.get("changed_at"))}</td>
          <td>{_esc(item.get("from_tier") or "—")}</td>
          <td><span class="admin-tier admin-tier--{ _esc(str(item.get("to_tier", "")).lower()) }">{_esc(item.get("to_tier"))}</span></td>
          <td>{_esc(item.get("score"))}</td>
          <td>{_esc(item.get("changed_by"))}</td>
        </tr>"""
        for item in tier_history
    ) or '<tr><td colspan="5">No tier changes recorded yet.</td></tr>'

    target_summary = ""
    if target:
        target_summary = f"""<dl class="admin-dl">
          <div><dt>Current tier</dt><dd><span class="admin-tier admin-tier--{ _esc(target.get("tier", "").lower()) }">{_esc(target.get("tier"))}</span></dd></div>
          <div><dt>Score</dt><dd>{_esc(target.get("score"))}</dd></div>
          <div><dt>Strongest signals</dt><dd>{_format_signals(target.get("strongest_signals"))}</dd></div>
          <div><dt>Warm path</dt><dd>{_esc(target.get("warm_path") or "—")}</dd></div>
          <div><dt>Evidence freshness</dt><dd>{_format_freshness(target.get("evidence_freshness"), stale_evidence=bool(target.get("stale_evidence")))}</dd></div>
          <div><dt>Missing fields</dt><dd>{_format_missing(target.get("missing_fields"))}</dd></div>
          <div><dt>Pipeline stage</dt><dd>{_esc(pipeline_stage_label(target.get("pipeline_stage")))}</dd></div>
        </dl>"""
    else:
        target_summary = '<p class="admin-hint">Score below active target threshold (4+). Not on tier A/B/C lists.</p>'

    main = f"""<section class="admin-section" aria-labelledby="target-detail-title">
      {banner_html}
      <p class="admin-breadcrumb"><a href="/admin/targets">Target lists</a> · <a href="/admin/companies/{_esc(company.get("id"))}">{_esc(company.get("name"))}</a></p>
      <h1 class="admin-title" id="target-detail-title">{_esc(company.get("name"))}</h1>
      <p class="admin-lede">Qualification tier history — distinct from pipeline stage promotion.</p>
      {target_summary}
      <section class="admin-subsection" aria-labelledby="tier-history-title">
        <h2 class="admin-subtitle" id="tier-history-title">Tier changes</h2>
        <div class="admin-table-wrap">
          <table class="admin-table">
            <thead><tr><th>Changed</th><th>From</th><th>To</th><th>Score</th><th>By</th></tr></thead>
            <tbody>{history_rows}</tbody>
          </table>
        </div>
      </section>
    </section>"""
    return render_admin_shell(
        title=f"Target — {company.get('name', '')}",
        main=main,
        active_path="/admin/targets",
        admin_username=admin_username,
        csrf_token=csrf_token,
    )
