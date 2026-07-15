"""Mock admin page data for ADMIN_PREVIEW_MODE screenshots.

Never used in production — only when ``Settings.admin_preview_enabled`` is true.
"""

from __future__ import annotations

import html
import os
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID

from app.acquisition_dashboard import (
    AcquisitionDashboardData,
    CompanyAttentionRow,
    CountBucket,
    EvidenceRow,
    NextActionRow,
)
from app.companies import COMPANY_CATEGORIES, COMPANY_STAGES


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
# Reserved preview detail id for ADMIN_PREVIEW_MODE database-unavailable screenshots.
PREVIEW_BRIEF_DATABASE_ERROR_ID = 503
# Brief already linked to CRM/pipeline in preview screenshots.
PREVIEW_BRIEF_CONVERTED_ID = 3
# Brief convert preview with explicit domain/email matches for Reviewer shots.
PREVIEW_BRIEF_CONVERT_MATCHES_ID = 4
PREVIEW_BRIEF_CONVERT_VALIDATION_ERROR = (
    "Select an existing company match or choose to create a new company."
)
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

# Section path → short column labels for preview tables.
_SECTION_COLUMNS: dict[str, tuple[str, ...]] = {
    "/admin/companies": ("Company", "Category", "Stage", "Target", "Verified"),
    "/admin/contacts": ("Name", "Roles", "Company", "Email", "Last touch"),
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


def _preview_uuid(rng: random.Random) -> str:
    return str(UUID(int=rng.getrandbits(128), version=4))


def build_preview_acquisition_dashboard_data(
    *,
    rng: random.Random | None = None,
    now: datetime | None = None,
) -> AcquisitionDashboardData:
    """Randomized acquisition dashboard for ADMIN_PREVIEW_MODE screenshots."""
    rng = rng or _preview_rng()
    now = now or datetime.now(timezone.utc)
    companies = list(COMPANY_NAMES)
    rng.shuffle(companies)

    def _buckets(registry: dict[str, str]) -> tuple[CountBucket, ...]:
        chosen = rng.sample(list(registry.keys()), k=min(len(registry), rng.randint(3, 5)))
        return tuple(
            CountBucket(key=key, label=registry[key], count=rng.randint(1, 18))
            for key in chosen
        )

    def _next_actions(*, overdue: bool) -> tuple[NextActionRow, ...]:
        rows: list[NextActionRow] = []
        for i in range(rng.randint(3, 6)):
            company = companies[i % len(companies)]
            first = rng.choice(CONTACT_FIRST)
            last = rng.choice(CONTACT_LAST)
            delta_days = rng.randint(1, 10)
            review_at = now - timedelta(days=delta_days) if overdue else now + timedelta(days=delta_days)
            rows.append(
                NextActionRow(
                    record_id=_preview_uuid(rng),
                    company_id=_preview_uuid(rng),
                    company_name=company,
                    contact_name=f"{first} {last}",
                    body=rng.choice(BRIEF_TEXTS)[:140],
                    review_at=review_at,
                )
            )
        return tuple(rows)

    def _evidence(*, stale: bool) -> tuple[EvidenceRow, ...]:
        rows: list[EvidenceRow] = []
        for i in range(rng.randint(3, 5)):
            company = companies[(i + 2) % len(companies)]
            created = now - timedelta(days=rng.randint(1, 20))
            expires = now - timedelta(days=rng.randint(1, 5)) if stale else now + timedelta(days=30)
            rows.append(
                EvidenceRow(
                    record_id=_preview_uuid(rng),
                    company_id=_preview_uuid(rng),
                    company_name=company,
                    record_type=rng.choice(("verified_fact", "public_signal")),
                    body=rng.choice(BRIEF_TEXTS)[:120],
                    created_at=created,
                    expires_at=expires,
                )
            )
        return tuple(rows)

    def _attention() -> tuple[CompanyAttentionRow, ...]:
        stage_keys = tuple(COMPANY_STAGES.keys())
        category_keys = tuple(COMPANY_CATEGORIES.keys())
        rows: list[CompanyAttentionRow] = []
        for i in range(rng.randint(2, 4)):
            company = companies[(i + 4) % len(companies)]
            stage = rng.choice(stage_keys)
            category = rng.choice(category_keys)
            rows.append(
                CompanyAttentionRow(
                    company_id=_preview_uuid(rng),
                    company_name=company,
                    target_status=rng.choice(("target", "watching")),
                    category=category,
                    stage=stage,
                )
            )
        return tuple(rows)

    return AcquisitionDashboardData(
        company_counts_by_stage=_buckets(COMPANY_STAGES),
        company_counts_by_category=_buckets(COMPANY_CATEGORIES),
        contact_counts_by_stage=_buckets(COMPANY_STAGES),
        contact_counts_by_category=_buckets(COMPANY_CATEGORIES),
        overdue_actions=_next_actions(overdue=True),
        upcoming_actions=_next_actions(overdue=False),
        recent_evidence=_evidence(stale=False),
        stale_evidence=_evidence(stale=True),
        without_decision_maker=_attention(),
        without_next_action=_attention(),
        generated_at=now,
    )


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
                    rng.choice(("Fintech", "AI infrastructure", "Digital assets", "Investor", "Other")),
                    rng.choice(("Pre-seed", "Seed", "Series A", "Series B+")),
                    rng.choice(("Target", "Watching", "Not a fit")),
                    stamp,
                )
            )
        elif active_path == "/admin/contacts":
            from app.contacts import BUYING_ROLES

            role_count = rng.randint(1, 3)
            roles = rng.sample(list(BUYING_ROLES.values()), k=min(role_count, len(BUYING_ROLES)))
            rows.append(
                (
                    person,
                    ", ".join(roles),
                    company,
                    _slug_email(first, last, company, rng),
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


def preview_pipeline_available() -> bool:
    return True


def preview_brief_conversion_state(brief_id: int) -> dict[str, object] | None:
    """Return linked CRM state for converted preview briefs."""
    if brief_id != PREVIEW_BRIEF_CONVERTED_ID:
        return None
    company_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    return {
        "company": {
            "id": company_id,
            "name": "Northwind Labs",
            "pipeline_stage": "diagnostic_paid",
        },
        "contact": {
            "id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            "email": "alex.nguyen@northwindlabs.io",
        },
        "pipeline_stage": "diagnostic_paid",
    }


def preview_brief_convert_matches(
    brief_id: int,
    *,
    price_cents: int,
) -> dict[str, object]:
    from app.brief_conversion import build_conversion_proposal

    brief = build_preview_brief_detail(brief_id)
    if brief is None:
        return {"proposal": {}, "company_matches": [], "contact_matches": []}
    proposal = build_conversion_proposal(dict(brief), price_cents=price_cents)
    company_matches: list[dict[str, object]] = []
    contact_matches: list[dict[str, object]] = []
    if brief_id in (1, PREVIEW_BRIEF_CONVERT_MATCHES_ID):
        company_matches.append(
            {
                "id": "cccccccc-cccc-cccc-cccc-cccccccccccc",
                "name": "Northwind Labs (existing)",
                "domain": proposal.get("domain"),
            }
        )
    if brief_id == PREVIEW_BRIEF_CONVERT_MATCHES_ID:
        contact_matches.append(
            {
                "id": "dddddddd-dddd-dddd-dddd-dddddddddddd",
                "email": proposal.get("contact_email"),
                "company_id": company_matches[0]["id"] if company_matches else None,
            }
        )
    return {
        "proposal": proposal,
        "company_matches": company_matches,
        "contact_matches": contact_matches,
    }


def preview_brief_convert_post(
    brief_id: int,
    *,
    company_mode: str,
    contact_mode: str,
    selected_company_id: object,
    selected_contact_id: object,
) -> str | None:
    """Simulate validation errors for preview POST; None means success."""
    if brief_id == PREVIEW_BRIEF_CONVERT_MATCHES_ID:
        if company_mode == "existing" and selected_company_id is None:
            return "Select an existing company match or choose to create a new company."
        if contact_mode == "existing" and selected_contact_id is None:
            return "Select the existing contact match or choose to create a new contact."
    if brief_id == PREVIEW_BRIEF_CONVERTED_ID:
        return None
    return None


AUDIT_ACTIONS = (
    "admin.login.success",
    "admin.logout",
    "import.batch",
    "entity.delete",
    "pipeline.update",
    "brief.convert",
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

