"""Admin HTML for lead discovery review inbox."""

from __future__ import annotations

import html
import json
from datetime import datetime
from typing import Any

from app.admin_layout import render_admin_shell
from app.companies import COMPANY_CATEGORIES
from app.discovery_inbox import (
    CONFIDENCE_FILTERS,
    DISCOVERY_BULK_MAX,
    DISCOVERY_FRESHNESS_FILTERS,
)


def _esc(value: Any) -> str:
    return "" if value is None else html.escape(str(value), quote=True)


def _format_timestamp(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M UTC")
    return _esc(value)


def _options(registry: dict[str, str], selected: str | None, *, empty: str = "Any") -> str:
    rows = [f'<option value="">{html.escape(empty)}</option>']
    rows.extend(
        f'<option value="{_esc(key)}"{" selected" if key == selected else ""}>{_esc(label)}</option>'
        for key, label in registry.items()
    )
    return "\n".join(rows)


def _review_state_options(selected: str | None) -> str:
    labels = {
        "pending": "Pending review",
        "deferred": "Deferred",
        "accepted": "Accepted",
        "rejected": "Rejected",
    }
    return _options(labels, selected, empty="Any state")


def _run_options(runs: list[dict[str, Any]], selected: str | None) -> str:
    rows = [f'<option value="">{html.escape("Any run")}</option>']
    for run in runs:
        run_id = str(run.get("id"))
        label = (
            f"{run.get('source_id')} · {_format_timestamp(run.get('started_at'))} "
            f"({run.get('candidate_count', 0)} candidates)"
        )
        rows.append(
            f'<option value="{_esc(run_id)}"{" selected" if run_id == selected else ""}>'
            f"{_esc(label)}</option>"
        )
    return "\n".join(rows)


def _source_options(sources: list[str], selected: str | None) -> str:
    rows = [f'<option value="">{html.escape("Any source")}</option>']
    rows.extend(
        f'<option value="{_esc(source)}"{" selected" if source == selected else ""}>'
        f"{_esc(source)}</option>"
        for source in sources
    )
    return "\n".join(rows)


def render_discovery_inbox_page(
    *,
    candidates: list[dict[str, Any]],
    filters: dict[str, str | None],
    filter_metadata: dict[str, Any],
    csrf_token: str,
    admin_username: str,
    preview_banner: str | None = None,
    status_message: str | None = None,
    error_message: str | None = None,
) -> str:
    banner_html = ""
    if preview_banner:
        banner_html = (
            f'<p class="admin-preview-banner" role="status">{_esc(preview_banner)}</p>'
        )
    status_html = ""
    if status_message:
        status_html = f'<p class="admin-status" role="status">{_esc(status_message)}</p>'
    elif error_message:
        status_html = f'<p class="form-error" role="alert">{_esc(error_message)}</p>'

    rows = "".join(
        f"""<tr>
          <td><input type="checkbox" name="candidate_ids" value="{_esc(row.get('id'))}" aria-label="Select {_esc(row.get('name'))}" /></td>
          <td><a href="/admin/discovery/inbox/{_esc(row.get('id'))}">{_esc(row.get('name'))}</a></td>
          <td>{_esc(row.get('source_id'))}</td>
          <td>{_esc(COMPANY_CATEGORIES.get(str(row.get('category')), row.get('category') or '—'))}</td>
          <td>{_esc(row.get('confidence') if row.get('confidence') is not None else '—')}</td>
          <td>{_esc(DISCOVERY_FRESHNESS_FILTERS.get(str(row.get('freshness')), row.get('freshness') or '—'))}</td>
          <td>{_esc(str(row.get('review_state', 'pending')).replace('_', ' ').title())}</td>
          <td>{_format_timestamp(row.get('discovered_at'))}</td>
        </tr>"""
        for row in candidates
    ) or (
        '<tr><td colspan="8">No candidates match these filters. '
        "Scheduled discovery runs populate this inbox for operator review.</td></tr>"
    )

    main = f"""<section class="admin-section" aria-labelledby="discovery-inbox-title">
      {banner_html}
      {status_html}
      <div class="admin-section-head">
        <div>
          <p class="admin-eyebrow">Lead discovery</p>
          <h1 class="admin-title" id="discovery-inbox-title">Review inbox</h1>
          <p class="admin-lede">Accept stores candidates for research in CRM. Reject suppresses identical evidence. Defer schedules a later review date.</p>
        </div>
      </div>
      <form class="admin-form admin-form--compact" method="get" action="/admin/discovery/inbox">
        <div class="field"><label for="source-filter">Source</label><select id="source-filter" name="source">{_source_options(filter_metadata.get('sources') or [], filters.get('source'))}</select></div>
        <div class="field"><label for="run-filter">Retrieval run</label><select id="run-filter" name="run_id">{_run_options(filter_metadata.get('runs') or [], filters.get('run_id'))}</select></div>
        <div class="field"><label for="category-filter">Category</label><select id="category-filter" name="category">{_options(COMPANY_CATEGORIES, filters.get('category'))}</select></div>
        <div class="field"><label for="confidence-filter">Confidence</label><select id="confidence-filter" name="confidence">{_options(CONFIDENCE_FILTERS, filters.get('confidence'))}</select></div>
        <div class="field"><label for="freshness-filter">Freshness</label><select id="freshness-filter" name="freshness">{_options(DISCOVERY_FRESHNESS_FILTERS, filters.get('freshness'))}</select></div>
        <div class="field"><label for="review-filter">Review state</label><select id="review-filter" name="review_state">{_review_state_options(filters.get('review_state'))}</select></div>
        <button class="cta admin-submit" type="submit">Filter</button>
      </form>
      <form class="admin-form" method="post" action="/admin/discovery/inbox/bulk/preview">
        <input type="hidden" name="csrf_token" value="{_esc(csrf_token)}" />
        <div class="admin-table-wrap">
          <table class="admin-table">
            <thead><tr>
              <th scope="col">Bulk</th>
              <th scope="col">Company</th><th scope="col">Source</th><th scope="col">Category</th>
              <th scope="col">Confidence</th><th scope="col">Freshness</th><th scope="col">State</th><th scope="col">Discovered</th>
            </tr></thead>
            <tbody>{rows}</tbody>
          </table>
        </div>
        <p class="admin-hint">Bulk actions are limited to {DISCOVERY_BULK_MAX} candidates and require a preview step.</p>
        <div class="admin-form-row">
          <div class="field"><label for="bulk-action">Bulk action</label>
            <select id="bulk-action" name="action" required>
              <option value="">Choose action…</option>
              <option value="accept">Accept (create company for research)</option>
              <option value="reject">Reject</option>
              <option value="defer">Defer</option>
            </select>
          </div>
          <button class="cta admin-submit" type="submit">Preview bulk action</button>
        </div>
      </form>
    </section>"""
    return render_admin_shell(
        title="Discovery inbox",
        main=main,
        active_path="/admin/discovery/inbox",
        admin_username=admin_username,
        csrf_token=csrf_token,
    )


def _render_evidence_block(evidence: Any) -> str:
    if evidence is None:
        return "<p>No supporting evidence attached.</p>"
    if isinstance(evidence, str):
        try:
            evidence = json.loads(evidence)
        except json.JSONDecodeError:
            return f"<pre>{_esc(evidence)}</pre>"
    if not isinstance(evidence, dict):
        return "<p>No supporting evidence attached.</p>"
    observations = evidence.get("observations") or []
    snippet = evidence.get("snippet")
    parts: list[str] = []
    if snippet:
        parts.append(f"<p><strong>Snippet:</strong> {_esc(snippet)}</p>")
    if observations:
        rows = "".join(
            f"""<tr>
              <td><a href="{_esc(obs.get('source_url'))}" rel="noopener noreferrer">{_esc(obs.get('source_url'))}</a></td>
              <td>{_esc(obs.get('value'))}</td>
              <td>{_esc(obs.get('confidence'))}</td>
              <td>{_format_timestamp(obs.get('retrieved_at'))}</td>
            </tr>"""
            for obs in observations
            if isinstance(obs, dict)
        )
        parts.append(
            f"""<div class="admin-table-wrap"><table class="admin-table">
              <thead><tr><th>Source</th><th>Observation</th><th>Confidence</th><th>Retrieved</th></tr></thead>
              <tbody>{rows}</tbody>
            </table></div>"""
        )
    return "".join(parts) or "<p>No supporting evidence attached.</p>"


def render_discovery_candidate_page(
    *,
    candidate: dict[str, Any],
    csrf_token: str,
    admin_username: str,
    preview_banner: str | None = None,
    error_message: str | None = None,
    status_message: str | None = None,
) -> str:
    banner_html = ""
    if preview_banner:
        banner_html = (
            f'<p class="admin-preview-banner" role="status">{_esc(preview_banner)}</p>'
        )
    status_html = ""
    if status_message:
        status_html = f'<p class="admin-status" role="status">{_esc(status_message)}</p>'
    elif error_message:
        status_html = f'<p class="form-error" role="alert">{_esc(error_message)}</p>'

    match_suggestions = candidate.get("match_suggestions") or []
    if isinstance(match_suggestions, str):
        match_suggestions = json.loads(match_suggestions)
    match_rows = "".join(
        f"""<tr>
          <td><a href="/admin/companies/{_esc(row.get('id'))}">{_esc(row.get('name'))}</a></td>
          <td>{_esc(row.get('domain') or '—')}</td>
        </tr>"""
        for row in match_suggestions
        if isinstance(row, dict)
    ) or "<tr><td colspan=\"2\">No CRM matches suggested.</td></tr>"

    conflicts = candidate.get("conflicts") or []
    if isinstance(conflicts, str):
        conflicts = json.loads(conflicts)
    conflict_items = "".join(
        f"<li>{_esc(item)}</li>" for item in conflicts if item
    ) or "<li>No field conflicts detected.</li>"

    linked = candidate.get("linked_company_id")
    linked_html = ""
    if linked:
        linked_html = (
            f'<p class="admin-hint">Linked company: '
            f'<a href="/admin/companies/{_esc(linked)}">{_esc(linked)}</a></p>'
        )

    reviewable = str(candidate.get("review_state") or "pending") in {"pending", "deferred"}
    action_forms = ""
    if reviewable:
        match_options = "".join(
            f'<option value="{_esc(row.get("id"))}">{_esc(row.get("name"))}</option>'
            for row in match_suggestions
            if isinstance(row, dict)
        )
        action_forms = f"""
        <section class="admin-subsection" aria-labelledby="accept-title">
          <h2 class="admin-subtitle" id="accept-title">Accept</h2>
          <p class="admin-hint">Acceptance stores the company for research — not sales-ready outreach.</p>
          <form class="admin-form" method="post" action="/admin/discovery/inbox/{_esc(candidate.get('id'))}/accept">
            <input type="hidden" name="csrf_token" value="{_esc(csrf_token)}" />
            <fieldset>
              <legend class="sr-only">Company choice</legend>
              <label><input type="radio" name="company_choice" value="new" checked /> Create new company</label>
              <label><input type="radio" name="company_choice" value="existing" /> Link existing company</label>
            </fieldset>
            <div class="field"><label for="selected-company">Existing company</label>
              <select id="selected-company" name="selected_company_id">
                <option value="">Select match…</option>
                {match_options}
              </select>
            </div>
            <button class="cta admin-submit" type="submit">Accept candidate</button>
          </form>
        </section>
        <section class="admin-subsection" aria-labelledby="reject-title">
          <h2 class="admin-subtitle" id="reject-title">Reject</h2>
          <form class="admin-form" method="post" action="/admin/discovery/inbox/{_esc(candidate.get('id'))}/reject">
            <input type="hidden" name="csrf_token" value="{_esc(csrf_token)}" />
            <div class="field"><label for="rejection-reason">Reason</label>
              <textarea id="rejection-reason" name="rejection_reason" required minlength="3" maxlength="500" placeholder="Why reject this candidate?"></textarea>
            </div>
            <button class="admin-action admin-action--destructive" type="submit">Reject and suppress identical evidence</button>
          </form>
        </section>
        <section class="admin-subsection" aria-labelledby="defer-title">
          <h2 class="admin-subtitle" id="defer-title">Defer</h2>
          <form class="admin-form" method="post" action="/admin/discovery/inbox/{_esc(candidate.get('id'))}/defer">
            <input type="hidden" name="csrf_token" value="{_esc(csrf_token)}" />
            <div class="field"><label for="deferred-until">Review again on</label>
              <input id="deferred-until" type="datetime-local" name="deferred_until" required />
            </div>
            <button class="cta admin-submit admin-action--secondary" type="submit">Defer review</button>
          </form>
        </section>"""

    main = f"""<section class="admin-section" aria-labelledby="candidate-title">
      {banner_html}
      {status_html}
      <div class="admin-section-head">
        <div>
          <p class="admin-eyebrow">Lead discovery</p>
          <h1 class="admin-title" id="candidate-title">{_esc(candidate.get('name'))}</h1>
          <p class="admin-lede">Proposed company from {_esc(candidate.get('source_id'))} · state {_esc(str(candidate.get('review_state')).replace('_', ' '))}</p>
        </div>
        <p><a href="/admin/discovery/inbox">← Back to inbox</a></p>
      </div>
      {linked_html}
      <dl class="admin-dl">
        <div><dt>Domain</dt><dd>{_esc(candidate.get('domain') or '—')}</dd></div>
        <div><dt>Website</dt><dd>{_esc(candidate.get('website') or '—')}</dd></div>
        <div><dt>Category</dt><dd>{_esc(COMPANY_CATEGORIES.get(str(candidate.get('category')), candidate.get('category') or '—'))}</dd></div>
        <div><dt>Confidence</dt><dd>{_esc(candidate.get('confidence') if candidate.get('confidence') is not None else '—')}</dd></div>
        <div><dt>Freshness</dt><dd>{_esc(DISCOVERY_FRESHNESS_FILTERS.get(str(candidate.get('freshness')), candidate.get('freshness') or '—'))}</dd></div>
        <div><dt>Discovered</dt><dd>{_format_timestamp(candidate.get('discovered_at'))}</dd></div>
        <div><dt>Signals</dt><dd>{_esc(', '.join(candidate.get('signals') or []) or '—')}</dd></div>
      </dl>
      <section class="admin-subsection" aria-labelledby="evidence-title">
        <h2 class="admin-subtitle" id="evidence-title">Supporting evidence</h2>
        {_render_evidence_block(candidate.get('evidence'))}
      </section>
      <section class="admin-subsection" aria-labelledby="conflicts-title">
        <h2 class="admin-subtitle" id="conflicts-title">Conflicts</h2>
        <ul>{conflict_items}</ul>
      </section>
      <section class="admin-subsection" aria-labelledby="matches-title">
        <h2 class="admin-subtitle" id="matches-title">Match suggestions</h2>
        <div class="admin-table-wrap"><table class="admin-table">
          <thead><tr><th>Company</th><th>Domain</th></tr></thead>
          <tbody>{match_rows}</tbody>
        </table></div>
      </section>
      {action_forms}
    </section>"""
    return render_admin_shell(
        title=f"Discovery · {_esc(candidate.get('name'))}",
        main=main,
        active_path="/admin/discovery/inbox",
        admin_username=admin_username,
        csrf_token=csrf_token,
    )


def render_discovery_bulk_preview_page(
    *,
    preview: dict[str, Any],
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
        error_html = f'<p class="form-error" role="alert">{_esc(error_message)}</p>'

    rows = "".join(
        f"""<tr>
          <td>{_esc(row.get('name'))}</td>
          <td>{_esc(row.get('source_id'))}</td>
          <td>{_esc(row.get('domain') or '—')}</td>
          <td>{_esc(str(row.get('review_state')).replace('_', ' '))}</td>
        </tr>"""
        for row in preview.get("candidates") or []
    )
    invalid = preview.get("invalid_state_ids") or []
    invalid_html = ""
    if invalid:
        invalid_html = (
            f'<p class="form-error" role="alert">'
            f"{len(invalid)} selected candidates are not pending and will be skipped.</p>"
        )

    extra_fields = ""
    action = str(preview.get("action") or "")
    if action == "reject":
        extra_fields = f"""
        <div class="field"><label for="bulk-rejection-reason">Rejection reason</label>
          <textarea id="bulk-rejection-reason" name="rejection_reason" required minlength="3" maxlength="500">{_esc(preview.get('rejection_reason') or '')}</textarea>
        </div>"""
    elif action == "defer":
        deferred = preview.get("deferred_until") or ""
        extra_fields = f"""
        <div class="field"><label for="bulk-deferred-until">Review again on</label>
          <input id="bulk-deferred-until" type="datetime-local" name="deferred_until" value="{_esc(deferred[:16] if deferred else '')}" required />
        </div>"""

    hidden_ids = "".join(
        f'<input type="hidden" name="candidate_ids" value="{_esc(row.get("id"))}" />'
        for row in preview.get("candidates") or []
    )

    main = f"""<section class="admin-section" aria-labelledby="bulk-preview-title">
      {banner_html}
      {error_html}
      {invalid_html}
      <div class="admin-section-head">
        <div>
          <p class="admin-eyebrow">Lead discovery</p>
          <h1 class="admin-title" id="bulk-preview-title">Bulk action preview</h1>
          <p class="admin-lede">Confirm {_esc(action)} for {preview.get('count', 0)} candidates. This step is auditable.</p>
        </div>
        <p><a href="/admin/discovery/inbox">← Back to inbox</a></p>
      </div>
      <div class="admin-table-wrap"><table class="admin-table">
        <thead><tr><th>Company</th><th>Source</th><th>Domain</th><th>State</th></tr></thead>
        <tbody>{rows}</tbody>
      </table></div>
      <form class="admin-form" method="post" action="/admin/discovery/inbox/bulk/commit">
        <input type="hidden" name="csrf_token" value="{_esc(csrf_token)}" />
        <input type="hidden" name="action" value="{_esc(action)}" />
        <input type="hidden" name="preview_token" value="{_esc(preview.get('preview_token'))}" />
        {hidden_ids}
        {extra_fields}
        <button class="cta admin-submit" type="submit">Confirm bulk {_esc(action)}</button>
      </form>
    </section>"""
    return render_admin_shell(
        title="Bulk preview",
        main=main,
        active_path="/admin/discovery/inbox",
        admin_username=admin_username,
        csrf_token=csrf_token,
    )
