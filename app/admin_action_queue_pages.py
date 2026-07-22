"""Admin HTML for the daily acquisition action queue."""

from __future__ import annotations

import html
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.acquisition_action_queue import (
    QUEUE_CATEGORY_DUE_TODAY,
    QUEUE_CATEGORY_OVERDUE,
    QUEUE_CATEGORY_STALE_EVIDENCE,
    QUEUE_CATEGORY_TIER_A,
    QUEUE_CATEGORY_WARM_INTRO,
    ActionQueueData,
    ActionQueueItem,
)
from app.admin_layout import render_admin_shell
from app.pipeline_stages import pipeline_stage_label


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _format_timestamp(value: datetime | None) -> str:
    if value is None:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo("UTC"))
    return _esc(value.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M UTC"))


_CATEGORY_LABELS: dict[str, str] = {
    QUEUE_CATEGORY_OVERDUE: "Overdue action",
    QUEUE_CATEGORY_DUE_TODAY: "Due today",
    QUEUE_CATEGORY_TIER_A: "Tier A qualified",
    QUEUE_CATEGORY_WARM_INTRO: "Warm introduction",
    QUEUE_CATEGORY_STALE_EVIDENCE: "Stale evidence",
}


def _entity_links(item: ActionQueueItem) -> str:
    links = [
        f'<a href="/admin/pipeline/{_esc(item.company_id)}">{_esc(item.company_name)}</a>',
        f'<a href="/admin/companies/{_esc(item.company_id)}">Company</a>',
    ]
    if item.contact_id:
        links.append(
            f'<a href="/admin/contacts/{_esc(item.contact_id)}">'
            f'{_esc(item.contact_name or "Contact")}</a>'
        )
    if item.evidence_record_id:
        links.append(
            f'<a href="/admin/companies/{_esc(item.company_id)}#research">Evidence</a>'
        )
    return " · ".join(links)


def _action_forms(item: ActionQueueItem, csrf_token: str) -> str:
    """Inline action forms for pipeline-linked queue items."""
    if item.category not in (
        QUEUE_CATEGORY_OVERDUE,
        QUEUE_CATEGORY_DUE_TODAY,
        QUEUE_CATEGORY_TIER_A,
    ):
        return (
            f'<form class="queue-action-form" method="post" action="/admin/queue/complete">'
            f'<input type="hidden" name="csrf_token" value="{_esc(csrf_token)}" />'
            f'<input type="hidden" name="company_id" value="{_esc(item.company_id)}" />'
            f'<input type="hidden" name="item_key" value="{_esc(item.item_key)}" />'
            f'<input type="hidden" name="item_category" value="{_esc(item.category)}" />'
            f'<button class="admin-action admin-action--secondary" type="submit">Mark done</button>'
            f"</form>"
        )
    due_value = ""
    if item.next_action_due_at:
        due_value = item.next_action_due_at.strftime("%Y-%m-%dT%H:%M")
    return f"""<div class="queue-actions">
      <form class="queue-action-form" method="post" action="/admin/queue/complete">
        <input type="hidden" name="csrf_token" value="{_esc(csrf_token)}" />
        <input type="hidden" name="company_id" value="{_esc(item.company_id)}" />
        <input type="hidden" name="item_key" value="{_esc(item.item_key)}" />
        <input type="hidden" name="item_category" value="{_esc(item.category)}" />
        <button class="admin-action admin-action--secondary" type="submit">Complete</button>
      </form>
      <form class="queue-action-form" method="post" action="/admin/queue/snooze">
        <input type="hidden" name="csrf_token" value="{_esc(csrf_token)}" />
        <input type="hidden" name="company_id" value="{_esc(item.company_id)}" />
        <input type="hidden" name="item_key" value="{_esc(item.item_key)}" />
        <input type="hidden" name="item_category" value="{_esc(item.category)}" />
        <label class="queue-inline-label">Snooze
          <select name="snooze_days">
            <option value="1">1 day</option>
            <option value="3" selected>3 days</option>
            <option value="7">7 days</option>
          </select>
        </label>
        <button class="admin-action admin-action--secondary" type="submit">Snooze</button>
      </form>
      <form class="queue-action-form" method="post" action="/admin/queue/reschedule">
        <input type="hidden" name="csrf_token" value="{_esc(csrf_token)}" />
        <input type="hidden" name="company_id" value="{_esc(item.company_id)}" />
        <input type="hidden" name="item_key" value="{_esc(item.item_key)}" />
        <input type="hidden" name="item_category" value="{_esc(item.category)}" />
        <label class="queue-inline-label">Due
          <input type="datetime-local" name="next_action_due_at" value="{_esc(due_value)}" required />
        </label>
        <button class="admin-action admin-action--secondary" type="submit">Reschedule</button>
      </form>
      <form class="queue-action-form queue-action-form--replace" method="post" action="/admin/queue/replace">
        <input type="hidden" name="csrf_token" value="{_esc(csrf_token)}" />
        <input type="hidden" name="company_id" value="{_esc(item.company_id)}" />
        <input type="hidden" name="item_key" value="{_esc(item.item_key)}" />
        <input type="hidden" name="item_category" value="{_esc(item.category)}" />
        <label class="queue-inline-label">Replace
          <input type="text" name="next_action" value="{_esc(item.next_action or '')}" required />
        </label>
        <label class="queue-inline-label">Due
          <input type="datetime-local" name="next_action_due_at" value="{_esc(due_value)}" required />
        </label>
        <button class="admin-action" type="submit">Replace</button>
      </form>
    </div>"""


def _render_queue_rows(items: tuple[ActionQueueItem, ...], *, csrf_token: str) -> str:
    if not items:
        return '<tr><td colspan="5" class="audit-empty">Queue is clear — no actions need attention.</td></tr>'
    rows: list[str] = []
    for item in items:
        stage = pipeline_stage_label(item.pipeline_stage) if item.pipeline_stage else "—"
        rows.append(
            f"""<tr>
              <td><span class="queue-priority">{item.priority_rank}</span> {_esc(_CATEGORY_LABELS.get(item.category, item.category))}</td>
              <td>{_entity_links(item)}</td>
              <td>{_esc(item.reason)}</td>
              <td>{_esc(stage)} · {_format_timestamp(item.next_action_due_at)}</td>
              <td>{_action_forms(item, csrf_token)}</td>
            </tr>"""
        )
    return "".join(rows)


def render_action_queue_page(
    *,
    data: ActionQueueData,
    admin_username: str,
    csrf_token: str,
    preview_banner: str | None = None,
    db_error: bool = False,
    action_message: str | None = None,
) -> str:
    generated = _format_timestamp(data.generated_at)
    banner_html = ""
    if preview_banner:
        banner_html = (
            f'<p class="admin-preview-banner" role="status">{_esc(preview_banner)}</p>'
        )
    message_html = ""
    if action_message:
        message_html = (
            f'<p class="admin-flash" role="status">{_esc(action_message)}</p>'
        )
    error_html = ""
    if db_error:
        error_html = """<p class="brief-error" role="alert">
            Action queue is temporarily unavailable. Try again shortly.
          </p>"""

    rules_html = "".join(
        f"<li><strong>{_esc(_CATEGORY_LABELS.get(key, key))}:</strong> {_esc(text)}</li>"
        for key, text in data.rules.items()
    )

    main = f"""<section class="admin-section queue-root" aria-labelledby="queue-title">
      {banner_html}
      {message_html}
      {error_html}
      <div class="admin-section-head">
        <div>
          <p class="admin-eyebrow">Acquisition</p>
          <h1 class="admin-title" id="queue-title">Daily action queue</h1>
          <p class="admin-lede">
            Prioritized work list for follow-ups, introductions, and evidence review.
            Generated <time datetime="{generated}">{generated}</time>.
          </p>
        </div>
        <form method="get" action="/admin/queue/export.csv">
          <button class="cta admin-submit" type="submit">Export spreadsheet</button>
        </form>
      </div>
      <details class="queue-rules">
        <summary>Prioritization rules</summary>
        <ul class="queue-rules-list">{rules_html}</ul>
      </details>
      <div class="admin-table-wrap">
        <table class="admin-table queue-table">
          <thead>
            <tr>
              <th scope="col">Priority</th>
              <th scope="col">Entity</th>
              <th scope="col">Why</th>
              <th scope="col">Stage / due</th>
              <th scope="col">Actions</th>
            </tr>
          </thead>
          <tbody>{_render_queue_rows(data.items, csrf_token=csrf_token)}</tbody>
        </table>
      </div>
    </section>"""
    return render_admin_shell(
        title="Action queue",
        main=main,
        active_path="/admin/queue",
        admin_username=admin_username,
        csrf_token=csrf_token,
    )
