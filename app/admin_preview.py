"""Mock admin page data for ADMIN_PREVIEW_MODE screenshots.

Never used in production — only when ``Settings.admin_preview_enabled`` is true.
"""

from __future__ import annotations

import html
import os
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


COMPANY_NAMES = (
    "Northwind Labs",
    "Helios Rail",
    "Cedar Protocol",
    "Aperture Freight",
    "Meridian Stack",
    "Volt Spiral",
    "Oakline Systems",
    "Bright Harbor",
    "Kite Ledger",
    "Summit Relay",
)

CONTACT_FIRST = (
    "Alex",
    "Sam",
    "Jordan",
    "Riley",
    "Casey",
    "Morgan",
    "Quinn",
    "Avery",
)

CONTACT_LAST = (
    "Nguyen",
    "Patel",
    "Okoro",
    "Berg",
    "Silva",
    "Khan",
    "Park",
    "Ellis",
)

STATUSES = ("new", "paid", "follow-up", "closed")
SOURCES = ("brief", "referral", "inbound", "partner")
SIGNAL_TYPES = ("hiring", "funding", "tech-stack", "intent", "news")
PIPELINE_STAGES = ("qualified", "discovery", "proposal", "negotiation", "won")
IMPORT_STATUSES = ("queued", "running", "complete", "failed")
CONTENT_KINDS = ("insight", "case-study", "landing", "brief copy")
BRIEF_PAYMENT_STATUSES = ("pending_payment", "paid", "abandoned")
BRIEF_TEXTS = (
    "Need a technical architecture review of our payments platform — "
    "API boundaries, retention, and rollout sequencing.",
    "Evaluating whether to rebuild the billing monolith or carve services. "
    "Want an outside view before Series B.",
    "Multi-tenant CRM sync is leaking PII across accounts. Diagnose root cause "
    "and propose a containment plan.",
    "Edge deploy latency spiked after the last k8s migration. Looking for a "
    "concrete diagnosis and go/no-go on rollback.",
)
UTM_SOURCES = ("linkedin", "referral", "google", "newsletter", "partner")
UTM_MEDIUMS = ("social", "cpc", "email", "organic", None)
UTM_CAMPAIGNS = ("spring-launch", "architecture-diagnostic", "inbound-q3", None)

PREVIEW_CONTACT_IDS = (
    "11111111-1111-1111-1111-111111111111",
    "22222222-2222-2222-2222-222222222222",
)

BUYING_ROLE_PREVIEW = (
    "founder",
    "technical_buyer",
    "executive_buyer",
    "influencer",
    "investor",
    "introducer",
    "other",
)

# Section path → short column labels for preview tables.
_SECTION_COLUMNS: dict[str, tuple[str, ...]] = {
    "/admin/companies": ("Company", "Industry", "Employees", "Owner", "Updated"),
    "/admin/contacts": ("Name", "Title", "Company", "Email", "Buying roles", "Last touch"),
    "/admin/signals": ("Signal", "Company", "Score", "Source", "Seen"),
    "/admin/pipeline": ("Deal", "Company", "Stage", "Value", "Next step"),
    "/admin/imports": ("Job", "Rows", "Status", "Source", "Started"),
    "/admin/discovery": ("List", "Prospects", "Filter", "Owner", "Refreshed"),
    "/admin/analytics": ("Metric", "Period", "Value", "Delta", "Segment"),
    "/admin/content": ("Title", "Kind", "Status", "Author", "Updated"),
    "/admin/settings": ("Setting", "Value", "Scope", "Owner", "Changed"),
}


@dataclass(frozen=True)
class PreviewBriefRow:
    company: str
    contact: str
    email: str
    status: str
    source: str
    submitted_at: str
    amount_cents: int


@dataclass(frozen=True)
class PreviewDashboardData:
    """Fake intake/CRM snapshot for screenshot preview."""

    briefs_this_week: int
    paid_this_week: int
    open_prospects: int
    sessions_active: int
    recent_briefs: tuple[PreviewBriefRow, ...]
    preview_banner: str
    generated_at: str


def _preview_rng() -> random.Random:
    """Randomize per process; optional ``ADMIN_PREVIEW_SEED`` for stable tests."""
    raw = (os.environ.get("ADMIN_PREVIEW_SEED") or "").strip()
    if raw:
        try:
            return random.Random(int(raw))
        except ValueError:
            return random.Random(raw)
    return random.Random()


def _slug_email(first: str, last: str, company: str, rng: random.Random) -> str:
    domain = company.lower().replace(" ", "") + rng.choice(
        (".io", ".com", ".co", ".dev")
    )
    return f"{first.lower()}.{last.lower()}@{domain}"


def build_preview_dashboard_data(
    *,
    rng: random.Random | None = None,
    now: datetime | None = None,
) -> PreviewDashboardData:
    """Build a randomized but plausible admin dashboard payload."""
    rng = rng or _preview_rng()
    now = now or datetime.now(timezone.utc)

    briefs_this_week = rng.randint(4, 28)
    paid_this_week = rng.randint(1, max(1, briefs_this_week // 2))
    open_prospects = rng.randint(6, 40)
    sessions_active = rng.randint(1, 4)

    row_count = rng.randint(4, 8)
    companies = list(COMPANY_NAMES)
    rng.shuffle(companies)
    rows: list[PreviewBriefRow] = []
    for i in range(row_count):
        company = companies[i % len(companies)]
        first = rng.choice(CONTACT_FIRST)
        last = rng.choice(CONTACT_LAST)
        hours_ago = rng.randint(1, 96)
        submitted = now - timedelta(hours=hours_ago, minutes=rng.randint(0, 50))
        rows.append(
            PreviewBriefRow(
                company=company,
                contact=f"{first} {last}",
                email=_slug_email(first, last, company, rng),
                status=rng.choice(STATUSES),
                source=rng.choice(SOURCES),
                submitted_at=submitted.strftime("%Y-%m-%d %H:%M UTC"),
                amount_cents=rng.choice((20_000, 20_000, 35_000, 50_000)),
            )
        )

    return PreviewDashboardData(
        briefs_this_week=briefs_this_week,
        paid_this_week=paid_this_week,
        open_prospects=open_prospects,
        sessions_active=sessions_active,
        recent_briefs=tuple(rows),
        preview_banner="Preview data — not production",
        generated_at=now.strftime("%Y-%m-%d %H:%M:%S UTC"),
    )


def _format_amount(cents: int) -> str:
    return f"${cents / 100:.0f}"


def render_preview_dashboard_main(data: PreviewDashboardData) -> str:
    """HTML main fragment for the admin shell dashboard in preview mode."""
    rows = "\n".join(
        (
            "<tr>"
            f"<td>{html.escape(row.company)}</td>"
            f"<td>{html.escape(row.contact)}</td>"
            f"<td>{html.escape(row.email)}</td>"
            f"<td><span class=\"admin-status admin-status-{html.escape(row.status)}\">"
            f"{html.escape(row.status)}</span></td>"
            f"<td>{html.escape(row.source)}</td>"
            f"<td>{html.escape(_format_amount(row.amount_cents))}</td>"
            f"<td>{html.escape(row.submitted_at)}</td>"
            "</tr>"
        )
        for row in data.recent_briefs
    )
    return f"""        <section class="admin-empty" aria-labelledby="admin-home-title">
          <p class="admin-preview-banner" role="status">{html.escape(data.preview_banner)}</p>
          <p class="admin-eyebrow">Preview</p>
          <h1 class="admin-title" id="admin-home-title">Dashboard</h1>
          <p class="admin-lede">
            Mock intake snapshot for screenshots
            (<time datetime="{html.escape(data.generated_at)}">{html.escape(data.generated_at)}</time>).
          </p>
          <dl class="admin-stat-row">
            <div>
              <dt>Briefs (7d)</dt>
              <dd>{data.briefs_this_week}</dd>
            </div>
            <div>
              <dt>Paid (7d)</dt>
              <dd>{data.paid_this_week}</dd>
            </div>
            <div>
              <dt>Open prospects</dt>
              <dd>{data.open_prospects}</dd>
            </div>
            <div>
              <dt>Active sessions</dt>
              <dd>{data.sessions_active}</dd>
            </div>
          </dl>
          <h2 class="admin-section-title">Recent submissions</h2>
          <div class="admin-table-wrap">
            <table class="admin-table">
              <thead>
                <tr>
                  <th scope="col">Company</th>
                  <th scope="col">Contact</th>
                  <th scope="col">Email</th>
                  <th scope="col">Status</th>
                  <th scope="col">Source</th>
                  <th scope="col">Amount</th>
                  <th scope="col">Submitted</th>
                </tr>
              </thead>
              <tbody>
                {rows}
              </tbody>
            </table>
          </div>
        </section>"""


def _person(rng: random.Random) -> str:
    return f"{rng.choice(CONTACT_FIRST)} {rng.choice(CONTACT_LAST)}"


def _relative_stamp(rng: random.Random, now: datetime) -> str:
    hours_ago = rng.randint(1, 120)
    stamp = now - timedelta(hours=hours_ago, minutes=rng.randint(0, 50))
    return stamp.strftime("%Y-%m-%d %H:%M UTC")


def build_preview_section_rows(
    active_path: str,
    *,
    rng: random.Random | None = None,
    now: datetime | None = None,
) -> tuple[tuple[str, ...], ...]:
    """Build randomized table rows for an admin section preview page."""
    rng = rng or _preview_rng()
    now = now or datetime.now(timezone.utc)
    companies = list(COMPANY_NAMES)
    rng.shuffle(companies)
    count = rng.randint(4, 8)
    rows: list[tuple[str, ...]] = []

    for i in range(count):
        company = companies[i % len(companies)]
        person = _person(rng)
        first, last = person.split(" ", 1)
        stamp = _relative_stamp(rng, now)
        if active_path == "/admin/companies":
            rows.append(
                (
                    company,
                    rng.choice(("Logistics", "SaaS", "Industrial", "Fintech")),
                    str(rng.choice((12, 28, 45, 80, 140))),
                    person,
                    stamp,
                )
            )
        elif active_path == "/admin/contacts":
            roles = ", ".join(
                rng.sample(
                    BUYING_ROLE_PREVIEW,
                    k=rng.randint(1, min(3, len(BUYING_ROLE_PREVIEW))),
                )
            )
            rows.append(
                (
                    person,
                    rng.choice(("CTO", "VP Eng", "Founder", "Ops lead")),
                    company,
                    _slug_email(first, last, company, rng),
                    roles,
                    stamp,
                )
            )
        elif active_path == "/admin/signals":
            rows.append(
                (
                    rng.choice(SIGNAL_TYPES),
                    company,
                    str(rng.randint(42, 98)),
                    rng.choice(SOURCES),
                    stamp,
                )
            )
        elif active_path == "/admin/pipeline":
            rows.append(
                (
                    f"{company.split()[0]} pilot",
                    company,
                    rng.choice(PIPELINE_STAGES),
                    _format_amount(rng.choice((20_000, 35_000, 50_000, 75_000))),
                    stamp,
                )
            )
        elif active_path == "/admin/imports":
            rows.append(
                (
                    f"import-{rng.randint(100, 999)}",
                    str(rng.randint(40, 2400)),
                    rng.choice(IMPORT_STATUSES),
                    rng.choice(("csv", "enrichment", "crm sync")),
                    stamp,
                )
            )
        elif active_path == "/admin/discovery":
            rows.append(
                (
                    f"{company.split()[0]} ICP",
                    str(rng.randint(18, 220)),
                    rng.choice(("geo+size", "tech stack", "hiring")),
                    person,
                    stamp,
                )
            )
        elif active_path == "/admin/analytics":
            rows.append(
                (
                    rng.choice(("Conversion", "Paid rate", "Time-to-reply", "Pipeline $")),
                    rng.choice(("7d", "30d", "90d")),
                    str(rng.randint(8, 96)),
                    f"{rng.choice(('+', '-'))}{rng.randint(1, 18)}%",
                    rng.choice(("all", "inbound", "partner")),
                )
            )
        elif active_path == "/admin/content":
            rows.append(
                (
                    f"{company} note",
                    rng.choice(CONTENT_KINDS),
                    rng.choice(("draft", "review", "published")),
                    person,
                    stamp,
                )
            )
        elif active_path == "/admin/settings":
            rows.append(
                (
                    rng.choice(
                        ("session TTL", "MFA required", "webhook URL", "timezone")
                    ),
                    rng.choice(("14d", "on", "https://hooks.example/…", "UTC")),
                    rng.choice(("security", "integrations", "team")),
                    person,
                    stamp,
                )
            )
        else:
            rows.append((company, person, stamp, rng.choice(STATUSES), "preview"))

    return tuple(rows)


def _brief_website(company: str, rng: random.Random) -> str:
    slug = company.lower().replace(" ", "-")
    tld = rng.choice((".io", ".com", ".co", ".dev"))
    path = rng.choice(("", "/platform", "/engineering", "/products"))
    return f"https://{slug}{tld}{path}"


def _brief_email(company: str, rng: random.Random) -> str:
    first = rng.choice(CONTACT_FIRST)
    last = rng.choice(CONTACT_LAST)
    return _slug_email(first, last, company, rng)


def build_preview_brief_rows(
    *,
    rng: random.Random | None = None,
    now: datetime | None = None,
) -> list[dict[str, object]]:
    """Randomized project-brief list rows for ADMIN_PREVIEW_MODE screenshots."""
    rng = rng or _preview_rng()
    now = now or datetime.now(timezone.utc)
    companies = list(COMPANY_NAMES)
    rng.shuffle(companies)
    count = rng.randint(5, 9)
    rows: list[dict[str, object]] = []
    for i in range(count):
        company = companies[i % len(companies)]
        brief_id = i + 1
        status = BRIEF_PAYMENT_STATUSES[0] if brief_id == 2 else rng.choice(BRIEF_PAYMENT_STATUSES)
        # Keep id=1 rich/paid and id=2 unpaid+nullable so Reviewer shots cover both AC states.
        if brief_id == 1:
            status = "paid"
        created = now - timedelta(hours=rng.randint(2, 120), minutes=rng.randint(0, 50))
        paid_at: datetime | None = None
        session_id: str | None = None
        intent_id: str | None = None
        if status == "paid":
            paid_at = created + timedelta(minutes=rng.randint(5, 90))
            session_id = f"cs_preview_{rng.randint(100000, 999999)}"
            intent_id = f"pi_preview_{rng.randint(100000, 999999)}"
        utm_source = None if brief_id == 2 else rng.choice(UTM_SOURCES)
        utm_medium = None if brief_id == 2 else rng.choice(UTM_MEDIUMS)
        utm_campaign = None if brief_id == 2 else rng.choice(UTM_CAMPAIGNS)
        utm_content = None if brief_id == 2 else rng.choice(("cta-primary", "hero", None))
        utm_term = None if brief_id == 2 else rng.choice(("architecture", "cto", None))
        website = (
            "https://very-long-subdomain-name.example.co.uk/path/to/resource?query=value"
            if brief_id == 2
            else _brief_website(company, rng)
        )
        brief_text = (
            ("A" * 220)
            + "\n\nSecond paragraph with <script>alert(1)</script> for escape checks."
            if brief_id == 2
            else rng.choice(BRIEF_TEXTS)
        )
        rows.append(
            {
                "id": brief_id,
                "created_at": created,
                "website": website,
                "contact_method": "email",
                "contact_value": _brief_email(company, rng),
                "brief": brief_text,
                "status": status,
                "stripe_session_id": session_id,
                "stripe_payment_intent_id": intent_id,
                "paid_at": paid_at,
                "utm_source": utm_source,
                "utm_medium": utm_medium,
                "utm_campaign": utm_campaign,
                "utm_content": utm_content,
                "utm_term": utm_term,
            }
        )
    return rows


def build_preview_brief_detail(
    brief_id: int,
    *,
    rng: random.Random | None = None,
    now: datetime | None = None,
) -> dict[str, object] | None:
    """Return one preview brief by id (from the seeded list), or None if unknown."""
    if brief_id < 1:
        return None
    # Fresh rng from the same seed so list and detail stay consistent.
    list_rng = rng if rng is not None else _preview_rng()
    rows = build_preview_brief_rows(rng=list_rng, now=now)
    for row in rows:
        if int(row["id"]) == brief_id:  # type: ignore[arg-type]
            return row
    return None


AUDIT_ACTIONS = (
    "admin.login.success",
    "admin.logout",
    "import.batch",
    "entity.delete",
    "pipeline.update",
)


def build_preview_audit_events(
    *,
    rng: random.Random | None = None,
    now: datetime | None = None,
) -> list[dict[str, object]]:
    """Randomized audit rows for ADMIN_PREVIEW_MODE screenshots."""
    rng = rng or _preview_rng()
    now = now or datetime.now(timezone.utc)
    count = rng.randint(4, 8)
    events: list[dict[str, object]] = []
    for i in range(count):
        created = now - timedelta(hours=rng.randint(1, 72), minutes=rng.randint(0, 50))
        action = rng.choice(AUDIT_ACTIONS)
        company = rng.choice(COMPANY_NAMES)
        actor = f"{rng.choice(CONTACT_FIRST).lower()}@saberistic.com"
        events.append(
            {
                "id": i + 1,
                "created_at": created,
                "actor": actor,
                "action": action,
                "entity_type": "company" if "pipeline" in action or "delete" in action else None,
                "entity_id": str(rng.randint(10, 99)) if "pipeline" in action else None,
                "correlation_id": f"corr-preview-{rng.randint(1000, 9999)}",
                "summary_before": {"name": company} if "update" in action else None,
                "summary_after": {"status": rng.choice(PIPELINE_STAGES)}
                if "update" in action
                else {"ok": True},
            }
        )
    return events


def render_preview_section_main(
    *,
    label: str,
    summary: str,
    active_path: str,
    rng: random.Random | None = None,
    now: datetime | None = None,
) -> str:
    """HTML main fragment for an admin section page in preview mode."""
    rng = rng or _preview_rng()
    now = now or datetime.now(timezone.utc)
    columns = _SECTION_COLUMNS.get(
        active_path, ("Item", "Detail", "Owner", "Status", "Updated")
    )
    data_rows = build_preview_section_rows(active_path, rng=rng, now=now)
    head = "".join(f"<th scope=\"col\">{html.escape(col)}</th>" for col in columns)
    body = "\n".join(
        "<tr>"
        + "".join(f"<td>{html.escape(cell)}</td>" for cell in row)
        + "</tr>"
        for row in data_rows
    )
    generated = now.strftime("%Y-%m-%d %H:%M:%S UTC")
    safe_label = html.escape(label)
    return f"""        <section class="admin-empty" aria-labelledby="admin-section-title">
          <p class="admin-preview-banner" role="status">Preview data — not production</p>
          <p class="admin-eyebrow">Preview</p>
          <h1 class="admin-title" id="admin-section-title">{safe_label}</h1>
          <p class="admin-lede">
            {html.escape(summary)} Mock rows for screenshots
            (<time datetime="{html.escape(generated)}">{html.escape(generated)}</time>).
          </p>
          <div class="admin-table-wrap">
            <table class="admin-table">
              <thead>
                <tr>
                  {head}
                </tr>
              </thead>
              <tbody>
                {body}
              </tbody>
            </table>
          </div>
        </section>"""


def build_preview_contact_detail(
    contact_key: int | str,
    *,
    rng: random.Random | None = None,
    now: datetime | None = None,
) -> dict[str, object] | None:
    """Preview contact rows: key 1 / first UUID full profile; key 2 sparse/nullables."""
    if isinstance(contact_key, str):
        try:
            contact_index = PREVIEW_CONTACT_IDS.index(contact_key) + 1
        except ValueError:
            return None
    else:
        contact_index = contact_key
    if contact_index < 1 or contact_index > len(PREVIEW_CONTACT_IDS):
        return None
    preview_id = PREVIEW_CONTACT_IDS[contact_index - 1]
    rng = rng or _preview_rng()
    now = now or datetime.now(timezone.utc)
    company = COMPANY_NAMES[(contact_index - 1) % len(COMPANY_NAMES)]
    first = CONTACT_FIRST[contact_index % len(CONTACT_FIRST)]
    last = CONTACT_LAST[contact_index % len(CONTACT_LAST)]
    full_name = f"{first} {last}"
    if contact_index == 1:
        return {
            "id": preview_id,
            "full_name": full_name,
            "title": "CTO",
            "company_id": contact_index,
            "company_name": company,
            "email": _slug_email(first, last, company, rng),
            "profile_url": f"https://linkedin.com/in/{first.lower()}-{last.lower()}",
            "email_provenance": "conference badge scan",
            "email_permission": "explicit opt-in",
            "relationship_strength": 4,
            "last_interaction_at": now - timedelta(days=3),
            "notes": "Warm intro path via portfolio founder.",
            "status": "active",
            "buying_roles": ["founder", "technical_buyer"],
        }
    return {
        "id": preview_id,
        "full_name": full_name,
        "title": None,
        "company_id": None,
        "company_name": None,
        "email": None,
        "profile_url": f"https://www.linkedin.com/in/{first.lower()}{last.lower()}/",
        "email_provenance": None,
        "email_permission": None,
        "relationship_strength": None,
        "last_interaction_at": None,
        "notes": None,
        "status": "active",
        "buying_roles": ["influencer"],
    }


def render_preview_contacts_main(
    *,
    admin_username: str = "",
    csrf_token: str = "",
    query: str | None = None,
    include_archived: bool = False,
    rng: random.Random | None = None,
    now: datetime | None = None,
) -> str:
    from app.admin_contacts_pages import render_admin_contacts_page

    rng = rng or _preview_rng()
    now = now or datetime.now(timezone.utc)
    rows: list[dict[str, object]] = []
    for index in range(rng.randint(5, 8)):
        company = COMPANY_NAMES[index % len(COMPANY_NAMES)]
        first = rng.choice(CONTACT_FIRST)
        last = rng.choice(CONTACT_LAST)
        rows.append(
            {
                "id": PREVIEW_CONTACT_IDS[index % len(PREVIEW_CONTACT_IDS)],
                "full_name": f"{first} {last}",
                "title": rng.choice(("CTO", "VP Eng", "Founder", "Ops lead")),
                "company_name": company,
                "email": _slug_email(first, last, company, rng),
                "buying_roles": rng.sample(
                    BUYING_ROLE_PREVIEW,
                    k=rng.randint(1, 3),
                ),
                "last_interaction_at": now - timedelta(hours=rng.randint(2, 120)),
                "status": "archived" if include_archived and index == 0 else "active",
            }
        )
    return render_admin_contacts_page(
        contacts=rows,  # type: ignore[arg-type]
        total=len(rows),
        query=query,
        include_archived=include_archived,
        csrf_token=csrf_token,
        admin_username=admin_username,
    )


def render_preview_contact_form(
    *,
    admin_username: str = "",
    csrf_token: str = "",
    is_edit: bool = False,
    contact_id: object | None = None,
    rng: random.Random | None = None,
) -> str:
    from app.admin_contacts_pages import render_admin_contact_form_page

    rng = rng or _preview_rng()
    companies = [{"id": index + 1, "name": name} for index, name in enumerate(COMPANY_NAMES[:6])]
    contact: dict[str, object] | None = None
    buying_roles: list[str] = []
    if is_edit:
        detail = build_preview_contact_detail(1, rng=rng)
        if detail is not None:
            contact = detail
            buying_roles = list(detail.get("buying_roles") or [])  # type: ignore[arg-type]
    return render_admin_contact_form_page(
        companies=companies,  # type: ignore[arg-type]
        contact=contact,
        buying_roles=buying_roles,
        csrf_token=csrf_token,
        admin_username=admin_username,
        is_edit=is_edit,
    )


def render_preview_contact_detail(
    *,
    contact_id: object,
    admin_username: str = "",
    csrf_token: str = "",
    error_message: str | None = None,
    rng: random.Random | None = None,
) -> str:
    from app.admin_research_pages import render_admin_contact_research_page

    try:
        numeric_id = PREVIEW_CONTACT_IDS.index(str(contact_id)) + 1
    except ValueError:
        try:
            numeric_id = int(str(contact_id))
        except ValueError:
            numeric_id = 1
    detail = build_preview_contact_detail(numeric_id, rng=rng)
    if detail is None:
        detail = build_preview_contact_detail(1, rng=rng) or {}
    company = None
    if detail.get("company_name"):
        company = {
            "id": detail.get("company_id", 1),
            "name": detail["company_name"],
        }
    records: list[dict[str, object]] = []
    if numeric_id == 1:
        records = [
            {
                "record_type": "relationship_context",
                "body": "Met at SaaStr — interested in architecture diagnostic.",
            }
        ]
    return render_admin_contact_research_page(
        contact=detail,  # type: ignore[arg-type]
        company=company,  # type: ignore[arg-type]
        records=records,  # type: ignore[arg-type]
        csrf_token=csrf_token,
        admin_username=admin_username,
        error_message=error_message,
    )
