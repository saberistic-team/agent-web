"""Mock admin page data for ADMIN_PREVIEW_MODE screenshots.

Never used in production — only when ``Settings.admin_preview_enabled`` is true.
"""

from __future__ import annotations

import html
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID

from app.admin_preview_context import (
    get_preview_context,
    preview_reference_time,
    preview_rng_for_namespace,
)

from app.acquisition_dashboard import (
    AcquisitionDashboardData,
    CompanyAttentionRow,
    CountBucket,
    EvidenceRow,
    NextActionRow,
)
from app.marketing_analytics_dashboard import (
    AttributionRow,
    ContentEngagementRow,
    ConversionRateRow,
    EventCountRow,
    MarketingAnalyticsDashboardData,
    normalize_filters,
)
from app.acquisition_action_queue import (
    QUEUE_CATEGORY_DUE_TODAY,
    QUEUE_CATEGORY_OVERDUE,
    QUEUE_CATEGORY_STALE_EVIDENCE,
    QUEUE_CATEGORY_TIER_A,
    QUEUE_CATEGORY_WARM_INTRO,
    ActionQueueData,
    ActionQueueItem,
    PRIORITY_RANK,
)
from app.pipeline_stages import PIPELINE_STAGES
from app.companies import COMPANY_CATEGORIES, COMPANY_STAGES, TARGET_STATUSES
from app.icp_scoring import default_icp_rules


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
IMPORT_STATUSES = ("queued", "running", "complete", "failed")
CONTENT_KINDS = ("insight", "case-study", "landing", "brief copy")
BRIEF_PAYMENT_STATUSES = ("pending_payment", "paid", "abandoned")
# Reserved preview detail id for ADMIN_PREVIEW_MODE database-unavailable screenshots.
PREVIEW_BRIEF_DATABASE_ERROR_ID = 503
# Brief already linked to CRM/pipeline in preview screenshots.
PREVIEW_BRIEF_CONVERTED_ID = 3
# Brief convert preview with explicit domain/email matches for Reviewer shots.
PREVIEW_BRIEF_CONVERT_MATCHES_ID = 4
# Brief convert preview with archived-only contact identity match (#276).
PREVIEW_BRIEF_CONVERT_ARCHIVED_MATCH_ID = 5
# Brief convert preview with no website/email data to match against at all (#276).
PREVIEW_BRIEF_CONVERT_EMPTY_ID = 6
# Brief convert preview with a website but no contact email on file (#276).
PREVIEW_BRIEF_CONVERT_NO_EMAIL_ID = 7
PREVIEW_BRIEF_CONVERT_VALIDATION_ERROR = (
    "Select an existing company match or choose to create a new company."
)
# Archived contact id for restore-conflict screenshots in ADMIN_PREVIEW_MODE.
PREVIEW_CONTACT_RESTORE_CONFLICT_ARCHIVED_ID = UUID(
    "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
)
PREVIEW_CONTACT_RESTORE_CONFLICT_ACTIVE_ID = UUID(
    "ffffffff-ffff-ffff-ffff-ffffffffffff"
)
# Company/contact detail pages for Archive and Restore screenshot states.
PREVIEW_COMPANY_DETAIL_ARCHIVE_ID = UUID("dddddddd-dddd-dddd-dddd-dddddddddd01")
PREVIEW_COMPANY_DETAIL_RESTORE_ID = UUID("dddddddd-dddd-dddd-dddd-dddddddddd02")
PREVIEW_CONTACT_DETAIL_ARCHIVE_ID = UUID("dddddddd-dddd-dddd-dddd-dddddddddd03")
PREVIEW_CONTACT_DETAIL_RESTORE_ID = UUID("dddddddd-dddd-dddd-dddd-dddddddddd04")
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

PREVIEW_PIPELINE_COMPANY_IDS = (
    UUID("11111111-1111-1111-1111-111111111111"),
    UUID("22222222-2222-2222-2222-222222222222"),
    UUID("33333333-3333-3333-3333-333333333333"),
    UUID("44444444-4444-4444-4444-444444444444"),
    UUID("55555555-5555-5555-5555-555555555555"),
)
# List fixtures for company/contact index screenshots (ADMIN_PREVIEW_MODE).
PREVIEW_COMPANY_IDS = (
    UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa01"),
    UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa02"),
    UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa03"),
    UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa04"),
    UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa05"),
    UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa06"),
)
PREVIEW_CONTACT_IDS = (
    UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbb01"),
    UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbb02"),
    UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbb03"),
    UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbb04"),
    UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbb05"),
    UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbb06"),
)
# Company/contact detail and editor screenshot fixtures (ADMIN_PREVIEW_MODE).
PREVIEW_COMPANY_POPULATED_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
PREVIEW_COMPANY_ARCHIVED_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa02")
PREVIEW_CONTACT_POPULATED_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
PREVIEW_CONTACT_ARCHIVED_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbc")
PREVIEW_QUALIFICATION_TARGET_IDS = (
    UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa01"),
    UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa02"),
    UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa03"),
    UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa04"),
    UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa05"),
)
PREVIEW_CONTACT_FOUNDER_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbd1")
PREVIEW_CONTACT_STALE_CTO_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbd2")
PREVIEW_CONTACT_INVESTOR_POSSIBLE_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbd3")
PREVIEW_CONTACT_INVESTOR_CONFIRMED_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbd4")
PREVIEW_CONTACT_INTRODUCER_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbd5")
PREVIEW_COMPANY_VALIDATION_ERROR = "Name must be at least 2 characters."
PREVIEW_PIPELINE_EXPECTED_VALUE_ERROR = (
    "Enter a whole number of cents (0 or greater)."
)
_SECTION_COLUMNS: dict[str, tuple[str, ...]] = {
    "/admin/companies": ("Company", "Category", "Stage", "Target", "Verified"),
    "/admin/contacts": ("Name", "Roles", "Company", "Email", "Last touch"),
    "/admin/signals": ("Company", "Score", "Version", "Type", "Calculated"),
    "/admin/targets": ("Tier", "Company", "Score", "Signals", "Freshness"),
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


def _resolve_rng(rng: random.Random | None, namespace: str) -> random.Random:
    if rng is not None:
        return rng
    return preview_rng_for_namespace(namespace)


def _resolve_now(now: datetime | None) -> datetime:
    if now is not None:
        return now
    return preview_reference_time()


def _preview_rng(namespace: str) -> random.Random:
    """Order-independent RNG for ADMIN_PREVIEW_MODE (namespace-scoped)."""
    return preview_rng_for_namespace(namespace)


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
    rng = _resolve_rng(rng, "acquisition_dashboard")
    now = _resolve_now(now)
    companies = list(COMPANY_NAMES)
    rng.shuffle(companies)

    def _buckets(registry: dict[str, str]) -> tuple[CountBucket, ...]:
        chosen = rng.sample(list(registry.keys()), k=min(len(registry), rng.randint(3, 5)))
        return tuple(
            CountBucket(key=key, label=registry[key], count=rng.randint(1, 18))
            for key in chosen
        )

    def _next_actions(*, overdue: bool) -> tuple[NextActionRow, ...]:
        stage_keys = list(PIPELINE_STAGES.keys())
        rows: list[NextActionRow] = []
        for i in range(rng.randint(3, 6)):
            company = companies[i % len(companies)]
            delta_days = rng.randint(1, 10)
            due_at = now - timedelta(days=delta_days) if overdue else now + timedelta(days=delta_days)
            rows.append(
                NextActionRow(
                    company_id=_preview_uuid(rng),
                    company_name=company,
                    pipeline_stage=stage_keys[i % len(stage_keys)],
                    pipeline_owner=rng.choice(("alex", "sam", "preview")),
                    next_action=rng.choice(BRIEF_TEXTS)[:140],
                    next_action_due_at=due_at,
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

    def _without_decision_maker() -> tuple[CompanyAttentionRow, ...]:
        """Uncovered targets for screenshots — lack qualifying active decision-makers."""
        stage_keys = tuple(COMPANY_STAGES.keys())
        category_keys = tuple(COMPANY_CATEGORIES.keys())
        uncovered_names = ("Meridian Stack", "Volt Spiral", "Aperture Freight")
        rows: list[CompanyAttentionRow] = []
        for i, company in enumerate(uncovered_names):
            rows.append(
                CompanyAttentionRow(
                    company_id=_preview_uuid(rng),
                    company_name=company,
                    target_status="target" if i == 0 else "watching",
                    category=category_keys[i % len(category_keys)],
                    stage=stage_keys[i % len(stage_keys)],
                )
            )
        return tuple(rows)

    def _attention(*, pipeline_only: bool = False) -> tuple[CompanyAttentionRow, ...]:
        stage_keys = tuple(COMPANY_STAGES.keys())
        category_keys = tuple(COMPANY_CATEGORIES.keys())
        pipeline_keys = tuple(PIPELINE_STAGES.keys())
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
                    pipeline_stage=rng.choice(pipeline_keys) if pipeline_only else None,
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
        without_decision_maker=_without_decision_maker(),
        without_next_action=_attention(pipeline_only=True),
        generated_at=now,
    )


def build_preview_marketing_analytics_data(
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    rng: random.Random | None = None,
    now: datetime | None = None,
) -> MarketingAnalyticsDashboardData:
    """Randomized marketing analytics dashboard for ADMIN_PREVIEW_MODE screenshots."""
    rng = _resolve_rng(rng, "marketing_analytics")
    now = _resolve_now(now)
    filters = normalize_filters(date_from=date_from, date_to=date_to, reference=now)

    engagement = (
        EventCountRow("Landing Viewed", rng.randint(120, 480), "browser"),
        EventCountRow("Services Viewed", rng.randint(40, 180), "browser"),
        EventCountRow("Case Studies Viewed", rng.randint(30, 120), "browser"),
        EventCountRow("Insights Viewed", rng.randint(25, 95), "browser"),
        EventCountRow("Brief Viewed", rng.randint(18, 72), "browser"),
        EventCountRow("Brief Form Started", rng.randint(8, 36), "browser"),
        EventCountRow("Contact Initiated", rng.randint(2, 14), "browser"),
    )
    server = (
        EventCountRow("Lead Persisted", rng.randint(4, 18), "server"),
        EventCountRow("Checkout Opened", rng.randint(3, 12), "server"),
        EventCountRow("Payment Completed", rng.randint(2, 9), "server"),
    )
    landing = engagement[0].count
    brief_viewed = engagement[4].count
    form_started = engagement[5].count
    leads = server[0].count
    checkouts = server[1].count
    payments = server[2].count

    conversion_rates = (
        ConversionRateRow(
            "Brief view → form start",
            form_started,
            brief_viewed,
            round(100.0 * form_started / brief_viewed, 1),
            "Count of `Brief Form Started` browser events",
            "Count of `Brief Viewed` browser events",
        ),
        ConversionRateRow(
            "Form start → lead",
            leads,
            form_started,
            round(100.0 * leads / form_started, 1),
            "Count of `Lead Persisted` server events",
            "Count of `Brief Form Started` browser events",
        ),
        ConversionRateRow(
            "Lead → checkout",
            checkouts,
            leads,
            round(100.0 * checkouts / leads, 1),
            "Count of `Checkout Opened` server events",
            "Count of `Lead Persisted` server events",
        ),
        ConversionRateRow(
            "Checkout → payment",
            payments,
            checkouts,
            round(100.0 * payments / checkouts, 1),
            "Count of `Payment Completed` server events",
            "Count of `Checkout Opened` server events",
        ),
        ConversionRateRow(
            "Landing → lead",
            leads,
            landing,
            round(100.0 * leads / landing, 1),
            "Count of `Lead Persisted` server events",
            "Count of `Landing Viewed` browser events",
        ),
    )

    attribution = (
        AttributionRow("linkedin", "social", "launch-q3", 86, 6, 3),
        AttributionRow("(direct)", "(none)", "(none)", 142, 4, 2),
        AttributionRow("newsletter", "email", "insights-digest", 38, 2, 1),
    )
    case_study_views = (
        ContentEngagementRow("northwind-labs", rng.randint(12, 48)),
        ContentEngagementRow("helios-rail", rng.randint(8, 32)),
        ContentEngagementRow("cedar-protocol", rng.randint(5, 24)),
    )
    article_views = (
        ContentEngagementRow("first-party-analytics", rng.randint(10, 40)),
        ContentEngagementRow("pipeline-attention", rng.randint(6, 28)),
    )

    return MarketingAnalyticsDashboardData(
        filters=filters,
        engagement_events=engagement,
        server_events=server,
        conversion_rates=conversion_rates,
        attribution=attribution,
        case_study_views=case_study_views,
        article_views=article_views,
        generated_at=now,
    )


def build_preview_action_queue_data(
    *,
    rng: random.Random | None = None,
    now: datetime | None = None,
) -> ActionQueueData:
    """Randomized daily action queue for ADMIN_PREVIEW_MODE screenshots."""
    rng = _resolve_rng(rng, "action_queue")
    now = _resolve_now(now)
    companies = list(COMPANY_NAMES)
    rng.shuffle(companies)
    stage_keys = list(PIPELINE_STAGES.keys())

    items: list[ActionQueueItem] = []

    # Overdue action
    items.append(
        ActionQueueItem(
            item_key=f"{QUEUE_CATEGORY_OVERDUE}:{_preview_uuid(rng)}",
            priority_rank=PRIORITY_RANK[QUEUE_CATEGORY_OVERDUE],
            category=QUEUE_CATEGORY_OVERDUE,
            reason=f"Overdue next action for {companies[0]} — due {(now - timedelta(days=3)).strftime('%Y-%m-%d %H:%M UTC')}.",
            company_id=_preview_uuid(rng),
            company_name=companies[0],
            next_action=rng.choice(BRIEF_TEXTS)[:100],
            next_action_due_at=now - timedelta(days=3),
            pipeline_stage=stage_keys[0],
            pipeline_owner="alex",
            expected_value_cents=120_000,
        )
    )

    # Due today
    items.append(
        ActionQueueItem(
            item_key=f"{QUEUE_CATEGORY_DUE_TODAY}:{_preview_uuid(rng)}",
            priority_rank=PRIORITY_RANK[QUEUE_CATEGORY_DUE_TODAY],
            category=QUEUE_CATEGORY_DUE_TODAY,
            reason=f"Next action due today for {companies[1]}.",
            company_id=_preview_uuid(rng),
            company_name=companies[1],
            next_action="Follow up on intro thread",
            next_action_due_at=now.replace(hour=17, minute=0),
            pipeline_stage=stage_keys[1],
            pipeline_owner="sam",
            expected_value_cents=80_000,
        )
    )

    # Tier A qualified
    items.append(
        ActionQueueItem(
            item_key=f"{QUEUE_CATEGORY_TIER_A}:{_preview_uuid(rng)}",
            priority_rank=PRIORITY_RANK[QUEUE_CATEGORY_TIER_A],
            category=QUEUE_CATEGORY_TIER_A,
            reason=f"Newly qualified Tier A target {companies[2]} (qualified {(now - timedelta(days=2)).strftime('%Y-%m-%d')}).",
            company_id=_preview_uuid(rng),
            company_name=companies[2],
            pipeline_stage="qualified",
            pipeline_owner="alex",
            expected_value_cents=200_000,
            qualified_at=now - timedelta(days=2),
        )
    )

    # Warm introduction
    contact_name = f"{rng.choice(CONTACT_FIRST)} {rng.choice(CONTACT_LAST)}"
    items.append(
        ActionQueueItem(
            item_key=f"{QUEUE_CATEGORY_WARM_INTRO}:{_preview_uuid(rng)}:{_preview_uuid(rng)}",
            priority_rank=PRIORITY_RANK[QUEUE_CATEGORY_WARM_INTRO],
            category=QUEUE_CATEGORY_WARM_INTRO,
            reason=f"Warm introduction path via {contact_name} at {companies[3]} (warm relationship).",
            company_id=_preview_uuid(rng),
            company_name=companies[3],
            contact_id=_preview_uuid(rng),
            contact_name=contact_name,
            pipeline_stage=stage_keys[2],
            expected_value_cents=60_000,
        )
    )

    # Stale high-value evidence
    items.append(
        ActionQueueItem(
            item_key=f"{QUEUE_CATEGORY_STALE_EVIDENCE}:{_preview_uuid(rng)}:{_preview_uuid(rng)}",
            priority_rank=PRIORITY_RANK[QUEUE_CATEGORY_STALE_EVIDENCE],
            category=QUEUE_CATEGORY_STALE_EVIDENCE,
            reason=f"Stale high-value evidence for {companies[4]} — confidence 85%, needs re-verification.",
            company_id=_preview_uuid(rng),
            company_name=companies[4],
            evidence_record_id=_preview_uuid(rng),
            evidence_confidence=0.85,
            evidence_source_url="https://example.com/signal",
            pipeline_stage=stage_keys[3],
            expected_value_cents=150_000,
        )
    )

    return ActionQueueData(items=tuple(items), generated_at=now)


def build_preview_export_csv() -> str:
    """Deterministic preview CSV with formula-injection sample cells."""
    from app.crm_export import EXPORT_COLUMNS, neutralize_csv_cell
    import csv
    import io

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(EXPORT_COLUMNS))
    writer.writeheader()
    writer.writerow(
        {
            "company_name": neutralize_csv_cell("=HYPERLINK(\"evil\")"),
            "company_domain": neutralize_csv_cell("northwind.io"),
            "pipeline_stage": "qualified",
            "tier": "A",
            "target_status": "target",
            "expected_value_usd": "1200.00",
            "next_action": neutralize_csv_cell("+cmd|'/c calc'"),
            "next_action_due_at": "2026-07-16 17:00 UTC",
            "contact_name": neutralize_csv_cell("Alex Chen"),
            "contact_title": "Founder",
            "contact_buying_roles": "founder",
            "contact_relationship_strength": "warm",
            "evidence_source_url": "https://example.com/evidence",
            "evidence_confidence": "0.85",
            "evidence_type": "verified_fact",
            "unresolved_fields": "",
        }
    )
    writer.writerow(
        {
            "company_name": neutralize_csv_cell("Helios Rail"),
            "company_domain": "",
            "pipeline_stage": "researching",
            "tier": "",
            "target_status": "watching",
            "expected_value_usd": "",
            "next_action": "",
            "next_action_due_at": "",
            "contact_name": "",
            "contact_title": "",
            "contact_buying_roles": "",
            "contact_relationship_strength": "",
            "evidence_source_url": "",
            "evidence_confidence": "",
            "evidence_type": "",
            "unresolved_fields": "next_action; next_action_due_at; decision_maker_contact",
        }
    )
    return buffer.getvalue()


def build_preview_dashboard_data(
    *,
    rng: random.Random | None = None,
    now: datetime | None = None,
) -> PreviewDashboardData:
    """Build a randomized but plausible admin dashboard payload."""
    rng = _resolve_rng(rng, "dashboard")
    now = _resolve_now(now)

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
    rng = _resolve_rng(rng, f"section:{active_path}")
    now = _resolve_now(now)
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
            stage_key = rng.choice(list(PIPELINE_STAGES))
            rows.append(
                (
                    f"{company.split()[0]} pilot",
                    company,
                    PIPELINE_STAGES[stage_key],
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


def build_preview_qualification_targets(
    *,
    filters: dict[str, str | None] | None = None,
    rng: random.Random | None = None,
    now: datetime | None = None,
) -> list[dict[str, object]]:
    """Randomized tier A/B/C targets for ADMIN_PREVIEW_MODE."""
    rng = _resolve_rng(rng, "qualification_targets")
    now = _resolve_now(now)
    filters = filters or {}
    tiers = ("A", "B", "C")
    targets: list[dict[str, object]] = []
    for index, company_id in enumerate(PREVIEW_QUALIFICATION_TARGET_IDS):
        tier = tiers[index % len(tiers)]
        score = {"A": 9, "B": 7, "C": 5}[tier]
        category = list(COMPANY_CATEGORIES)[index % len(COMPANY_CATEGORIES)]
        stage = list(COMPANY_STAGES)[index % len(COMPANY_STAGES)]
        pipeline_stage = list(PIPELINE_STAGES)[index % len(PIPELINE_STAGES)]
        owner = rng.choice(("alex", "sam", "preview"))
        has_warm = index % 2 == 0
        freshness = rng.choice(("fresh", "stale", "unknown", "mixed"))
        row: dict[str, object] = {
            "company_id": str(company_id),
            "id": company_id,
            "name": COMPANY_NAMES[index % len(COMPANY_NAMES)],
            "score": score,
            "tier": tier,
            "stage": stage,
            "vertical": category,
            "strongest_signals": [
                rng.choice(
                    (
                        "Target vertical",
                        "Funding stage fit",
                        "Warm introduction path",
                        "Fresh verified evidence",
                    )
                )
            ],
            "warm_path": f"{rng.choice(CONTACT_FIRST)} {rng.choice(CONTACT_LAST)} (introducer)"
            if has_warm
            else None,
            "has_warm_path": has_warm,
            "next_action": rng.choice(
                ("Review evidence gaps", "Request intro", "Validate headcount", None)
            ),
            "evidence_freshness": freshness,
            "missing_fields": ["company.headcount_estimate"] if index % 3 == 0 else [],
            "pipeline_stage": pipeline_stage,
            "pipeline_owner": owner,
            "score_calculated_at": now,
            "stale_evidence": freshness in {"stale", "mixed"},
        }
        if filters.get("tier") and row["tier"] != filters["tier"]:
            continue
        if filters.get("category") and row["vertical"] != filters["category"]:
            continue
        if filters.get("stage") and row["stage"] != filters["stage"]:
            continue
        if filters.get("pipeline_stage") and row["pipeline_stage"] != filters["pipeline_stage"]:
            continue
        if filters.get("owner") and str(row["pipeline_owner"]).lower() != str(filters["owner"]).lower():
            continue
        if filters.get("freshness") and row["evidence_freshness"] != filters["freshness"]:
            continue
        if filters.get("warm_path") == "yes" and not row["has_warm_path"]:
            continue
        if filters.get("warm_path") == "no" and row["has_warm_path"]:
            continue
        targets.append(row)
    return targets


def build_preview_qualification_working_lists(
    *,
    rng: random.Random | None = None,
    now: datetime | None = None,
) -> list[dict[str, object]]:
    rng = _resolve_rng(rng, "qualification_working_lists")
    now = _resolve_now(now)
    return [
        {
            "id": UUID(int=rng.getrandbits(128), version=4),
            "name": "Q3 Tier A shortlist",
            "item_count": 3,
            "updated_at": now - timedelta(days=2),
        },
        {
            "id": UUID(int=rng.getrandbits(128), version=4),
            "name": "Warm-path follow-ups",
            "item_count": 2,
            "updated_at": now - timedelta(days=5),
        },
    ]


def preview_qualification_target_exists(company_id: UUID) -> bool:
    return company_id in PREVIEW_QUALIFICATION_TARGET_IDS


def build_preview_qualification_target_detail(
    company_id: UUID,
    *,
    rng: random.Random | None = None,
    now: datetime | None = None,
) -> tuple[dict[str, object], dict[str, object] | None, list[dict[str, object]]]:
    rng = _resolve_rng(rng, f"qualification_target_detail:{company_id}")
    now = _resolve_now(now)
    targets = build_preview_qualification_targets(rng=rng, now=now)
    target = next((row for row in targets if row["id"] == company_id), None)
    company = {
        "id": company_id,
        "name": target["name"] if target else COMPANY_NAMES[0],
    }
    history = [
        {
            "changed_at": now - timedelta(days=14),
            "from_tier": "B",
            "to_tier": target["tier"] if target else "A",
            "score": target["score"] if target else 9,
            "changed_by": "preview",
        },
        {
            "changed_at": now - timedelta(days=30),
            "from_tier": None,
            "to_tier": "B",
            "score": 7,
            "changed_by": "system",
        },
    ]
    return company, target, history


def build_preview_pipeline_companies(
    *,
    stage_filter: str | None = None,
    rng: random.Random | None = None,
    now: datetime | None = None,
) -> list[dict[str, object]]:
    """Randomized pipeline companies for ADMIN_PREVIEW_MODE."""
    rng = _resolve_rng(rng, "pipeline_companies")
    now = _resolve_now(now)
    stage_keys = list(PIPELINE_STAGES)
    companies: list[dict[str, object]] = []
    for index, company_id in enumerate(PREVIEW_PIPELINE_COMPANY_IDS):
        stage_key = stage_keys[index % len(stage_keys)]
        if stage_filter and stage_key != stage_filter:
            continue
        due_offset = rng.randint(-5, 12)
        companies.append(
            {
                "id": company_id,
                "name": COMPANY_NAMES[index % len(COMPANY_NAMES)],
                "pipeline_stage": stage_key,
                "expected_value_cents": rng.choice((25_000, 50_000, 75_000, 120_000)),
                "next_action": rng.choice(
                    (
                        "Send diagnostic proposal",
                        "Schedule discovery call",
                        "Follow up on reply",
                        None,
                    )
                ),
                "next_action_due_at": now + timedelta(days=due_offset),
                "pipeline_owner": rng.choice(("alex", "sam", "preview")),
            }
        )
    return companies


def build_preview_companies(
    *,
    query: str | None = None,
    category: str | None = None,
    stage: str | None = None,
    target_status: str | None = None,
    freshness: str | None = None,
    include_archived: bool = False,
    rng: random.Random | None = None,
    now: datetime | None = None,
) -> list[dict[str, object]]:
    """Randomized company rows for ADMIN_PREVIEW_MODE list screenshots."""
    rng = _resolve_rng(rng, "companies")
    now = _resolve_now(now)
    category_keys = list(COMPANY_CATEGORIES)
    stage_keys = list(COMPANY_STAGES)
    target_keys = list(TARGET_STATUSES)
    companies: list[dict[str, object]] = []
    for index, company_id in enumerate(PREVIEW_COMPANY_IDS):
        name = COMPANY_NAMES[index % len(COMPANY_NAMES)]
        verified_days = rng.randint(-120, 45)
        verified_at = (now + timedelta(days=verified_days)).date().isoformat()
        archived_at = None
        if index == len(PREVIEW_COMPANY_IDS) - 1:
            archived_at = (now - timedelta(days=rng.randint(3, 30))).isoformat()
        companies.append(
            {
                "id": company_id,
                "name": name,
                "category": category_keys[index % len(category_keys)],
                "stage": stage_keys[index % len(stage_keys)],
                "target_status": target_keys[index % len(target_keys)],
                "last_verified_at": verified_at if verified_days >= -90 else None,
                "archived_at": archived_at,
            }
        )

    def _matches(row: dict[str, object]) -> bool:
        if query:
            needle = query.lower()
            if needle not in str(row.get("name", "")).lower():
                return False
        if category and row.get("category") != category:
            return False
        if stage and row.get("stage") != stage:
            return False
        if target_status and row.get("target_status") != target_status:
            return False
        if freshness == "fresh" and not row.get("last_verified_at"):
            return False
        if freshness == "stale" and row.get("last_verified_at"):
            return False
        if freshness == "unknown" and row.get("last_verified_at"):
            return False
        if not include_archived and row.get("archived_at"):
            return False
        return True

    return [row for row in companies if _matches(row)]


def build_preview_contacts(
    *,
    query: str | None = None,
    company_id: UUID | None = None,
    buying_role: str | None = None,
    include_archived: bool = False,
    rng: random.Random | None = None,
    now: datetime | None = None,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Randomized contact rows and company options for ADMIN_PREVIEW_MODE."""
    from app.contacts import BUYING_ROLES

    now = _resolve_now(now)
    if rng is not None:
        contact_rng = rng
        companies = build_preview_companies(rng=rng, now=now, include_archived=True)
    else:
        contact_rng = _resolve_rng(None, "contacts")
        companies = build_preview_companies(now=now, include_archived=True)
    company_by_id = {row["id"]: row for row in companies}
    role_keys = list(BUYING_ROLES)
    contacts: list[dict[str, object]] = []
    for index, contact_id in enumerate(PREVIEW_CONTACT_IDS):
        company = companies[index % len(companies)]
        first = CONTACT_FIRST[index % len(CONTACT_FIRST)]
        last = CONTACT_LAST[index % len(CONTACT_LAST)]
        company_name = str(company["name"])
        role_count = contact_rng.randint(1, 2)
        buying_roles = contact_rng.sample(role_keys, k=min(role_count, len(role_keys)))
        archived_at = None
        if index == len(PREVIEW_CONTACT_IDS) - 1:
            archived_at = (now - timedelta(days=contact_rng.randint(3, 30))).isoformat()
        contacts.append(
            {
                "id": contact_id,
                "full_name": f"{first} {last}",
                "title": contact_rng.choice(("CTO", "VP Engineering", "Founder", "Head of Product")),
                "buying_roles": buying_roles,
                "company_id": company["id"],
                "company_name": company_name,
                "email": _slug_email(first, last, company_name, contact_rng),
                "last_interaction_at": (now - timedelta(days=contact_rng.randint(1, 90))).date().isoformat(),
                "archived_at": archived_at,
            }
        )

    def _matches(row: dict[str, object]) -> bool:
        if query:
            needle = query.lower()
            haystack = " ".join(
                str(row.get(key, ""))
                for key in ("full_name", "title", "email")
            ).lower()
            if needle not in haystack:
                return False
        if company_id and row.get("company_id") != company_id:
            return False
        if buying_role and buying_role not in (row.get("buying_roles") or []):
            return False
        if not include_archived and row.get("archived_at"):
            return False
        return True

    filtered = [row for row in contacts if _matches(row)]
    return filtered, list(company_by_id.values())


def build_preview_pipeline_detail(
    company_id: UUID,
    *,
    rng: random.Random | None = None,
    now: datetime | None = None,
) -> tuple[dict[str, object], list[dict[str, object]], list[dict[str, object]]] | None:
    """Preview pipeline detail for a fixed company id."""
    now = _resolve_now(now)
    companies = build_preview_pipeline_companies(rng=rng, now=now)
    company = next((row for row in companies if row["id"] == company_id), None)
    if company is None:
        return None
    stage = str(company["pipeline_stage"])
    history = [
        {
            "changed_at": now - timedelta(days=14),
            "from_stage": "researching",
            "to_stage": "qualified",
            "changed_by": "preview",
        },
        {
            "changed_at": now - timedelta(days=7),
            "from_stage": "qualified",
            "to_stage": stage,
            "changed_by": "preview",
        },
    ]
    activities = [
        {
            "created_at": now - timedelta(days=3),
            "activity_type": "outreach",
            "summary": "Sent intro email with diagnostic overview.",
        },
        {
            "created_at": now - timedelta(days=1),
            "activity_type": "reply",
            "summary": "Prospect asked for pricing and timeline.",
        },
    ]
    if company_id == PREVIEW_PIPELINE_COMPANY_IDS[1]:
        company = {
            **company,
            "next_action": None,
            "next_action_due_at": None,
            "pipeline_owner": None,
            "expected_value_cents": None,
        }
    return company, history, activities


def build_preview_company(
    company_id: UUID,
    *,
    rng: random.Random | None = None,
    now: datetime | None = None,
) -> dict[str, object] | None:
    """Return one preview company row for detail/editor screenshots."""
    now = _resolve_now(now)
    company_rng = rng if rng is not None else _resolve_rng(None, f"company:{company_id}")
    if company_id == PREVIEW_COMPANY_POPULATED_ID:
        return {
            "id": company_id,
            "name": (
                "Northwind Labs — Enterprise Platform Modernization "
                "and Multi-Region Payments Advisory"
            ),
            "domain": "northwindlabs.io",
            "website": "https://northwindlabs.io/platform/engineering",
            "category": "fintech",
            "stage": "series_b_plus",
            "headcount_estimate": 240,
            "funding_summary": "Series B · $48M · 2025",
            "target_status": "target",
            "last_verified_at": (now - timedelta(days=12)).date().isoformat(),
            "notes": (
                "Primary diagnostic prospect. Long notes field for screenshot "
                "overflow checks across desktop and mobile viewports."
            ),
            "archived_at": None,
        }
    if company_id == PREVIEW_COMPANY_ARCHIVED_ID:
        return {
            "id": company_id,
            "name": "Helios Rail (archived)",
            "domain": "heliosrail.co",
            "website": "https://heliosrail.co",
            "category": "other",
            "stage": "seed",
            "headcount_estimate": None,
            "funding_summary": None,
            "target_status": "not_a_fit",
            "last_verified_at": None,
            "notes": None,
            "archived_at": (now - timedelta(days=21)).isoformat(),
        }
    if rng is not None:
        pipeline = build_preview_pipeline_companies(rng=rng, now=now)
    else:
        pipeline = build_preview_pipeline_companies(now=now)
    match = next((row for row in pipeline if row["id"] == company_id), None)
    if match is not None:
        return {
            "id": company_id,
            "name": match["name"],
            "domain": None,
            "website": None,
            "category": None,
            "stage": None,
            "headcount_estimate": None,
            "funding_summary": None,
            "target_status": None,
            "last_verified_at": None,
            "notes": None,
            "archived_at": None,
        }
    return None


def build_preview_companies_for_select(
    *,
    rng: random.Random | None = None,
    now: datetime | None = None,
) -> list[dict[str, object]]:
    """Companies for contact form company pickers in preview mode."""
    populated = build_preview_company(PREVIEW_COMPANY_POPULATED_ID, rng=rng, now=now)
    assert populated is not None
    rows = [populated]
    for company_id in PREVIEW_PIPELINE_COMPANY_IDS[:2]:
        row = build_preview_company(company_id, rng=rng, now=now)
        if row is not None:
            rows.append(row)
    return rows


def build_preview_company_contacts(
    company_id: UUID,
    *,
    rng: random.Random | None = None,
    now: datetime | None = None,
) -> list[dict[str, object]]:
    """Contacts linked to a preview company detail page."""
    if company_id != PREVIEW_COMPANY_POPULATED_ID:
        return []
    rng = _resolve_rng(rng, "company_contacts:populated")
    now = _resolve_now(now)
    populated = build_preview_contact(PREVIEW_CONTACT_POPULATED_ID, rng=rng, now=now)
    assert populated is not None
    first = rng.choice(CONTACT_FIRST)
    last = rng.choice(CONTACT_LAST)
    return [
        {
            "id": str(PREVIEW_CONTACT_FOUNDER_ID),
            "full_name": f"{first} {last}",
            "title": "Co-founder & CEO",
            "profile_url": f"https://linkedin.com/in/{first.lower()}-{last.lower()}-founder",
            "email": _slug_email(first, last, "Northwind", rng),
            "email_permission": "permitted",
            "company_id": str(company_id),
            "buying_roles": ["founder"],
            "relationship_strength": "strong",
            "last_interaction_at": (now - timedelta(days=3)).date().isoformat(),
            "archived_at": None,
        },
        populated,
        {
            "id": str(PREVIEW_CONTACT_STALE_CTO_ID),
            "full_name": "Morgan Ellis",
            "title": "Former CTO",
            "profile_url": "https://linkedin.com/in/morgan-ellis-former",
            "email": None,
            "email_permission": "unknown",
            "company_id": str(company_id),
            "buying_roles": ["technical_buyer"],
            "relationship_strength": "cold",
            "last_interaction_at": None,
            "archived_at": None,
        },
        {
            "id": str(PREVIEW_CONTACT_INVESTOR_POSSIBLE_ID),
            "full_name": "Riley Park",
            "title": "Partner",
            "profile_url": "https://linkedin.com/in/riley-park",
            "email": None,
            "email_permission": "unknown",
            "company_id": str(company_id),
            "buying_roles": ["investor"],
            "relationship_strength": "developing",
            "last_interaction_at": None,
            "archived_at": None,
        },
        {
            "id": str(PREVIEW_CONTACT_INVESTOR_CONFIRMED_ID),
            "full_name": "Casey Berg",
            "title": "Board observer",
            "profile_url": "https://linkedin.com/in/casey-berg",
            "email": "casey.berg@sequoia.example",
            "email_permission": "inferred",
            "company_id": str(company_id),
            "buying_roles": ["investor"],
            "relationship_strength": "warm",
            "last_interaction_at": (now - timedelta(days=12)).date().isoformat(),
            "archived_at": None,
        },
        {
            "id": str(PREVIEW_CONTACT_INTRODUCER_ID),
            "full_name": "Avery Silva",
            "title": "Advisor",
            "profile_url": "https://linkedin.com/in/avery-silva",
            "email": "avery.silva@example.com",
            "email_permission": "permitted",
            "company_id": str(company_id),
            "buying_roles": ["introducer"],
            "relationship_strength": "warm",
            "last_interaction_at": (now - timedelta(days=9)).date().isoformat(),
            "archived_at": None,
        },
    ]


def build_preview_company_research(
    company_id: UUID,
    *,
    rng: random.Random | None = None,
    now: datetime | None = None,
) -> list[dict[str, object]]:
    """Research records with public-evidence controls for screenshot fixtures."""
    if company_id != PREVIEW_COMPANY_POPULATED_ID:
        return []
    now = _resolve_now(now)
    return [
        {
            "record_type": "verified_fact",
            "body": "Raised Series B and hiring senior platform engineers.",
            "source_name": "TechCrunch",
            "source_url": "https://techcrunch.com/example/northwind-series-b",
            "observed_value": "48000000",
            "observed_at": (now - timedelta(days=30)).isoformat(),
            "confidence": 0.92,
            "review_at": (now + timedelta(days=30)).isoformat(),
            "expires_at": (now + timedelta(days=120)).isoformat(),
        },
        {
            "record_type": "public_signal",
            "body": "Job postings mention Kubernetes migration and PCI scope reduction.",
            "source_name": "LinkedIn Jobs",
            "source_url": "https://www.linkedin.com/jobs/view/1234567890",
            "observed_value": "12 open roles",
            "observed_at": (now - timedelta(days=4)).isoformat(),
            "confidence": 0.78,
            "review_at": (now + timedelta(days=14)).isoformat(),
            "expires_at": (now + timedelta(days=60)).isoformat(),
        },
        {
            "record_type": "verified_fact",
            "body": "Casey Berg listed as lead investor on the Series B announcement.",
            "contact_id": str(PREVIEW_CONTACT_INVESTOR_CONFIRMED_ID),
            "source_name": "Press release",
            "source_url": "https://northwindlabs.io/news/series-b",
            "observed_value": "Lead investor: Casey Berg",
            "observed_at": (now - timedelta(days=20)).isoformat(),
            "confidence": 0.95,
            "review_at": (now + timedelta(days=40)).isoformat(),
            "expires_at": (now + timedelta(days=180)).isoformat(),
        },
        {
            "record_type": "verified_fact",
            "body": "Morgan Ellis departed the CTO role; platform lead now interim.",
            "contact_id": str(PREVIEW_CONTACT_STALE_CTO_ID),
            "source_name": "Company blog",
            "source_url": "https://northwindlabs.io/blog/leadership-update",
            "observed_value": "CTO departed",
            "observed_at": (now - timedelta(days=45)).isoformat(),
            "confidence": 0.88,
            "review_at": (now + timedelta(days=30)).isoformat(),
            "expires_at": (now + timedelta(days=120)).isoformat(),
        },
        {
            "record_type": "relationship_context",
            "body": (
                "Former colleague at Cedar Protocol — worked together on payments "
                "platform for three years; offered to intro VP Engineering."
            ),
            "contact_id": str(PREVIEW_CONTACT_INTRODUCER_ID),
            "source_name": None,
            "source_url": None,
            "observed_value": None,
            "observed_at": None,
            "confidence": None,
            "review_at": None,
            "expires_at": None,
        },
        {
            "record_type": "hypothesis",
            "body": "Likely evaluating outside architecture review before Q4 platform freeze.",
            "source_name": None,
            "source_url": None,
            "observed_value": None,
            "observed_at": None,
            "confidence": None,
            "review_at": None,
            "expires_at": None,
        },
    ]


def _preview_relationship_metrics(
    rng: random.Random,
    now: datetime,
    *,
    two_way: bool,
    inbound: int,
    outbound: int,
) -> dict[str, object]:
    from app.linkedin_relationship_metrics import relationship_scoring_inputs

    last_days = rng.randint(3, 45)
    connection_days = rng.randint(120, 800)
    last_at = (now - timedelta(days=last_days)).date()
    connection_at = (now - timedelta(days=connection_days)).date()
    first_at = min(connection_at, last_at - timedelta(days=rng.randint(10, 90)))
    metrics: dict[str, object] = {
        "schema_version": "linkedin_relationship_v1",
        "connection_date": connection_at.isoformat(),
        "conversation_count": rng.randint(1, 4),
        "message_count": inbound + outbound,
        "inbound_count": inbound,
        "outbound_count": outbound,
        "first_interaction_at": first_at.isoformat(),
        "last_interaction_at": last_at.isoformat(),
        "recent_interaction_30d": last_days <= 30,
        "recent_interaction_90d": last_days <= 90,
        "two_way_conversation": two_way,
        "message_keys": [f"conv-{rng.randint(1, 9)}|{last_at.isoformat()}|Preview|Owner"],
        "updated_at": now.isoformat(),
        "reference_date": now.date().isoformat(),
    }
    metrics["scoring_inputs"] = relationship_scoring_inputs(metrics)
    return metrics


def build_preview_contact(
    contact_id: UUID,
    *,
    rng: random.Random | None = None,
    now: datetime | None = None,
) -> dict[str, object] | None:
    """Return one preview contact row for detail/editor screenshots."""
    now = _resolve_now(now)
    contact_rng = rng if rng is not None else _resolve_rng(None, f"contact:{contact_id}")
    if contact_id == PREVIEW_CONTACT_POPULATED_ID:
        first = contact_rng.choice(CONTACT_FIRST)
        last = contact_rng.choice(CONTACT_LAST)
        company = build_preview_company(PREVIEW_COMPANY_POPULATED_ID, rng=rng, now=now)
        company_name = str(company["name"]) if company else "Northwind Labs"
        return {
            "id": contact_id,
            "full_name": f"{first} {last}",
            "title": "VP Engineering",
            "profile_url": f"https://linkedin.com/in/{first.lower()}-{last.lower()}",
            "email": _slug_email(first, last, company_name.split("—")[0].strip(), contact_rng),
            "email_permission": "permitted",
            "company_id": PREVIEW_COMPANY_POPULATED_ID,
            "company_name": company_name,
            "buying_roles": ["technical_buyer"],
            "relationship_strength": "warm",
            "crm_context_tags": ["former_colleague"],
            "relationship_metrics": _preview_relationship_metrics(
                contact_rng,
                now,
                two_way=True,
                inbound=4,
                outbound=3,
            ),
            "last_interaction_at": (now - timedelta(days=6)).date().isoformat(),
            "notes": "Primary technical buyer; prefers async email before calls.",
            "archived_at": None,
        }
    if contact_id == PREVIEW_CONTACT_ARCHIVED_ID:
        return {
            "id": contact_id,
            "full_name": "Jordan Ellis (archived)",
            "title": "Former CTO",
            "profile_url": "https://linkedin.com/in/jordan-ellis-archived",
            "email": "jordan.ellis@heliosrail.co",
            "email_permission": "unknown",
            "company_id": PREVIEW_COMPANY_ARCHIVED_ID,
            "company_name": "Helios Rail (archived)",
            "buying_roles": ["founder"],
            "relationship_strength": "cold",
            "last_interaction_at": None,
            "notes": None,
            "archived_at": (now - timedelta(days=45)).isoformat(),
        }
    return None


def build_preview_contact_research(
    contact_id: UUID,
    *,
    rng: random.Random | None = None,
    now: datetime | None = None,
) -> list[dict[str, object]]:
    """Research records for contact detail screenshots."""
    if contact_id != PREVIEW_CONTACT_POPULATED_ID:
        return []
    now = _resolve_now(now)
    return [
        {
            "record_type": "relationship_context",
            "body": "Replied to intro email; asked for diagnostic scope and timeline.",
            "source_name": None,
            "source_url": None,
            "observed_value": None,
            "observed_at": None,
            "confidence": None,
            "review_at": None,
            "expires_at": None,
        },
        {
            "record_type": "follow_up_note",
            "body": "Schedule follow-up after they review the architecture brief.",
            "source_name": "CRM",
            "source_url": None,
            "observed_value": None,
            "observed_at": (now - timedelta(days=1)).isoformat(),
            "confidence": None,
            "review_at": None,
            "expires_at": None,
        },
    ]


def preview_company_fixture_ids() -> frozenset[UUID]:
    return frozenset({PREVIEW_COMPANY_POPULATED_ID, PREVIEW_COMPANY_ARCHIVED_ID})


def preview_contact_fixture_ids() -> frozenset[UUID]:
    return frozenset({PREVIEW_CONTACT_POPULATED_ID, PREVIEW_CONTACT_ARCHIVED_ID})


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
    rng = _resolve_rng(rng, "brief_rows")
    now = _resolve_now(now)
    companies = list(COMPANY_NAMES)
    rng.shuffle(companies)
    # Floor raised to 7 so ids 1-7 (including the #276 empty/no-email convert
    # preview fixtures below) are always present, regardless of the random draw.
    count = rng.randint(7, 9)
    rows: list[dict[str, object]] = []
    for i in range(count):
        company = companies[i % len(companies)]
        brief_id = i + 1
        status = BRIEF_PAYMENT_STATUSES[0] if brief_id == 2 else rng.choice(BRIEF_PAYMENT_STATUSES)
        created = now - timedelta(hours=rng.randint(2, 120), minutes=rng.randint(0, 50))
        paid_at: datetime | None = None
        session_id: str | None = None
        intent_id: str | None = None
        payment_subtotal_cents: int | None = None
        payment_discount_cents: int | None = None
        payment_amount_cents: int | None = None
        payment_currency: str | None = None
        stripe_promotion_code_id: str | None = None
        stripe_coupon_id: str | None = None
        # Keep id=1 full-price paid, id=2 unpaid+nullable, id=4 discounted paid.
        if brief_id == 1:
            status = "paid"
            paid_at = created + timedelta(minutes=rng.randint(5, 90))
            session_id = f"cs_preview_{rng.randint(100000, 999999)}"
            intent_id = f"pi_preview_{rng.randint(100000, 999999)}"
            payment_subtotal_cents = 20_000
            payment_amount_cents = 20_000
            payment_currency = "usd"
        elif brief_id == PREVIEW_BRIEF_CONVERT_MATCHES_ID:
            status = "paid"
            paid_at = created + timedelta(minutes=rng.randint(5, 90))
            session_id = f"cs_preview_{rng.randint(100000, 999999)}"
            intent_id = f"pi_preview_{rng.randint(100000, 999999)}"
            payment_subtotal_cents = 20_000
            payment_discount_cents = 5_000
            payment_amount_cents = 15_000
            payment_currency = "usd"
            stripe_promotion_code_id = "promo_preview_25off"
            stripe_coupon_id = "coupon_preview_25off"
        elif brief_id == PREVIEW_BRIEF_CONVERT_ARCHIVED_MATCH_ID:
            status = "paid"
            paid_at = created + timedelta(minutes=rng.randint(5, 90))
            session_id = f"cs_preview_{rng.randint(100000, 999999)}"
            intent_id = f"pi_preview_{rng.randint(100000, 999999)}"
            payment_subtotal_cents = 20_000
            payment_amount_cents = 20_000
            payment_currency = "usd"
        elif brief_id in (PREVIEW_BRIEF_CONVERT_EMPTY_ID, PREVIEW_BRIEF_CONVERT_NO_EMAIL_ID):
            status = "paid"
            paid_at = created + timedelta(minutes=rng.randint(5, 90))
            session_id = f"cs_preview_{rng.randint(100000, 999999)}"
            intent_id = f"pi_preview_{rng.randint(100000, 999999)}"
            payment_subtotal_cents = 20_000
            payment_amount_cents = 20_000
            payment_currency = "usd"
        elif status == "paid":
            paid_at = created + timedelta(minutes=rng.randint(5, 90))
            session_id = f"cs_preview_{rng.randint(100000, 999999)}"
            intent_id = f"pi_preview_{rng.randint(100000, 999999)}"
            payment_subtotal_cents = 20_000
            payment_amount_cents = 20_000
            payment_currency = "usd"
        utm_source = None if brief_id == 2 else rng.choice(UTM_SOURCES)
        utm_medium = None if brief_id == 2 else rng.choice(UTM_MEDIUMS)
        utm_campaign = None if brief_id == 2 else rng.choice(UTM_CAMPAIGNS)
        utm_content = None if brief_id == 2 else rng.choice(("cta-primary", "hero", None))
        utm_term = None if brief_id == 2 else rng.choice(("architecture", "cto", None))
        website = (
            "https://very-long-subdomain-name.example.co.uk/path/to/resource?query=value"
            if brief_id == 2
            else ""
            if brief_id == PREVIEW_BRIEF_CONVERT_EMPTY_ID
            else _brief_website(company, rng)
        )
        brief_text = (
            ("A" * 220)
            + "\n\nSecond paragraph with <script>alert(1)</script> for escape checks."
            if brief_id == 2
            else rng.choice(BRIEF_TEXTS)
        )
        contact_value = (
            ""
            if brief_id in (PREVIEW_BRIEF_CONVERT_EMPTY_ID, PREVIEW_BRIEF_CONVERT_NO_EMAIL_ID)
            else _brief_email(company, rng)
        )
        rows.append(
            {
                "id": brief_id,
                "created_at": created,
                "website": website,
                "contact_method": "email",
                "contact_value": contact_value,
                "brief": brief_text,
                "status": status,
                "stripe_session_id": session_id,
                "stripe_payment_intent_id": intent_id,
                "paid_at": paid_at,
                "payment_subtotal_cents": payment_subtotal_cents,
                "payment_discount_cents": payment_discount_cents,
                "payment_amount_cents": payment_amount_cents,
                "payment_currency": payment_currency,
                "stripe_promotion_code_id": stripe_promotion_code_id,
                "stripe_coupon_id": stripe_coupon_id,
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
    list_rng = rng if rng is not None else _preview_rng("brief_rows")
    rows = build_preview_brief_rows(rng=list_rng, now=_resolve_now(now))
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
        return {
            "proposal": {},
            "company_matches": [],
            "contact_matches": [],
            "archived_contact_match": None,
        }
    proposal = build_conversion_proposal(dict(brief), price_cents=price_cents)
    company_matches: list[dict[str, object]] = []
    contact_matches: list[dict[str, object]] = []
    archived_contact_match: dict[str, object] | None = None
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
    if brief_id == PREVIEW_BRIEF_CONVERT_ARCHIVED_MATCH_ID:
        archived_contact_match = {
            "id": "eeeeeeee-eeee-eeee-eeee-eeeeeeeeee05",
            "full_name": "Alex Nguyen (archived)",
            "email": proposal.get("contact_email"),
            "company_name": "Northwind Labs",
            "archived_at": "2026-01-15T14:30:00+00:00",
        }
    return {
        "proposal": proposal,
        "company_matches": company_matches,
        "contact_matches": contact_matches,
        "archived_contact_match": archived_contact_match,
    }


def preview_brief_convert_post(
    brief_id: int,
    *,
    company_mode: str,
    contact_mode: str,
    selected_company_id: object,
    selected_contact_id: object,
    acknowledge_archived_identity: bool = False,
) -> str | None:
    """Simulate validation errors for preview POST; None means success."""
    from app.brief_conversion import ARCHIVED_CONTACT_ACK_REQUIRED_MESSAGE

    if brief_id == PREVIEW_BRIEF_CONVERT_MATCHES_ID:
        if company_mode == "existing" and selected_company_id is None:
            return "Select an existing company match or choose to create a new company."
        if contact_mode == "existing" and selected_contact_id is None:
            return "Select the existing contact match or choose to create a new contact."
    if brief_id == PREVIEW_BRIEF_CONVERT_ARCHIVED_MATCH_ID:
        if contact_mode not in {"new", "existing"}:
            return "Choose whether to create or link a contact."
        if contact_mode == "new":
            matches = preview_brief_convert_matches(brief_id, price_cents=20_000)
            if matches.get("archived_contact_match") and not acknowledge_archived_identity:
                return ARCHIVED_CONTACT_ACK_REQUIRED_MESSAGE
    if brief_id == PREVIEW_BRIEF_CONVERTED_ID:
        return None
    return None


def build_preview_company_detail(
    company_id: UUID,
    *,
    rng: random.Random | None = None,
    now: datetime | None = None,
) -> tuple[dict[str, object], list[dict[str, object]], list[dict[str, object]]]:
    """Mock company detail data for Archive/Restore screenshot states."""
    rng = _resolve_rng(rng, f"company_detail:{company_id}")
    now = _resolve_now(now)
    archived = company_id == PREVIEW_COMPANY_DETAIL_RESTORE_ID
    company_name = rng.choice(COMPANY_NAMES)
    company: dict[str, object] = {
        "id": str(company_id),
        "name": company_name,
        "domain": f"{company_name.lower().replace(' ', '')}.io",
        "category": rng.choice(tuple(COMPANY_CATEGORIES.keys())),
        "stage": rng.choice(tuple(COMPANY_STAGES.keys())),
        "target_status": "target",
        "headcount_estimate": rng.randint(12, 240),
        "funding_summary": f"Seed — ${rng.randint(2, 18)}M",
        "last_verified_at": (now - timedelta(days=rng.randint(3, 45))).date().isoformat(),
    }
    if archived:
        company["archived_at"] = (now - timedelta(days=rng.randint(7, 90))).isoformat()
    first = rng.choice(CONTACT_FIRST)
    last = rng.choice(CONTACT_LAST)
    contacts = [
        {
            "id": str(PREVIEW_CONTACT_DETAIL_ARCHIVE_ID),
            "full_name": f"{first} {last}",
            "title": "VP Engineering",
            "email": _slug_email(first, last, company_name, rng),
            "buying_roles": ["technical_buyer"],
        }
    ]
    records = [
        {
            "record_type": "verified_fact",
            "body": f"{company_name} raised a seed round and is hiring platform engineers.",
            "source_name": "Press release",
            "source_url": f"https://{company['domain']}/news/seed",
            "observed_value": "Seed announced",
            "observed_at": now - timedelta(days=rng.randint(1, 20)),
            "confidence": 0.9,
            "review_at": now + timedelta(days=30),
            "expires_at": now + timedelta(days=120),
        }
    ]
    return company, contacts, records


def build_preview_contact_detail(
    contact_id: UUID,
    *,
    rng: random.Random | None = None,
    now: datetime | None = None,
) -> tuple[dict[str, object], dict[str, object] | None, list[dict[str, object]]]:
    """Mock contact detail/edit data for Archive/Restore screenshot states."""
    rng = _resolve_rng(rng, f"contact_detail:{contact_id}")
    now = _resolve_now(now)
    archived = contact_id == PREVIEW_CONTACT_DETAIL_RESTORE_ID
    first = rng.choice(CONTACT_FIRST)
    last = rng.choice(CONTACT_LAST)
    company_name = rng.choice(COMPANY_NAMES)
    contact: dict[str, object] = {
        "id": str(contact_id),
        "full_name": f"{first} {last}",
        "title": "VP Engineering",
        "profile_url": f"https://linkedin.com/in/{first.lower()}-{last.lower()}",
        "email": _slug_email(first, last, company_name, rng),
        "email_permission": "explicit_opt_in",
        "buying_roles": ["technical_buyer", "founder"],
        "relationship_strength": "warm",
        "crm_context_tags": ["warm_introducer"],
        "relationship_metrics": _preview_relationship_metrics(
            rng,
            now,
            two_way=False,
            inbound=1,
            outbound=5,
        ),
        "last_interaction_at": (now - timedelta(days=rng.randint(2, 30))).date().isoformat(),
        "notes": "Met at fintech infra meetup; interested in architecture review.",
        "company_id": str(PREVIEW_COMPANY_DETAIL_ARCHIVE_ID),
    }
    if archived:
        contact["archived_at"] = (now - timedelta(days=rng.randint(7, 60))).isoformat()
    company = {
        "id": str(PREVIEW_COMPANY_DETAIL_ARCHIVE_ID),
        "name": company_name,
    }
    records = [
        {
            "record_type": "signal",
            "body": f"{first} posted about scaling payments infrastructure.",
            "source_name": "LinkedIn",
            "source_url": str(contact["profile_url"]),
            "observed_value": "Hiring signal",
            "observed_at": now - timedelta(days=rng.randint(1, 14)),
            "confidence": 0.75,
            "review_at": now + timedelta(days=21),
            "expires_at": now + timedelta(days=90),
        }
    ]
    return contact, company, records


def preview_contact_restore_conflict(
    *,
    rng: random.Random | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    """Mock archived/active pair for contact restore-conflict screenshots."""
    rng = _resolve_rng(rng, "contact_restore_conflict")
    now = _resolve_now(now)
    first = rng.choice(CONTACT_FIRST)
    last = rng.choice(CONTACT_LAST)
    company = rng.choice(COMPANY_NAMES)
    email = _slug_email(first, last, company, rng)
    return {
        "archived_contact": {
            "id": str(PREVIEW_CONTACT_RESTORE_CONFLICT_ARCHIVED_ID),
            "full_name": f"{first} {last}",
            "title": "Former VP Engineering",
            "email": email,
            "company_name": company,
            "archived_at": (now - timedelta(days=rng.randint(1, 30))).isoformat(),
        },
        "conflicting_contact": {
            "contact_id": str(PREVIEW_CONTACT_RESTORE_CONFLICT_ACTIVE_ID),
            "full_name": f"{first} {last} (current)",
            "title": "CTO",
            "company_name": company,
            "company_id": "cccccccc-cccc-cccc-cccc-cccccccccccc",
        },
    }


AUDIT_ACTIONS = (
    "auth.login.success",
    "auth.logout",
    "import.batch",
    "entity.delete",
    "company.create",
    "company.update",
    "company.archive",
    "company.restore",
    "contact.create",
    "contact.update",
    "contact.archive",
    "contact.restore",
    "pipeline.update",
    "brief.convert",
    "research_record.create",
    "pipeline_activity.create",
)


def build_preview_audit_events(
    *,
    rng: random.Random | None = None,
    now: datetime | None = None,
) -> list[dict[str, object]]:
    """Randomized audit rows for ADMIN_PREVIEW_MODE screenshots."""
    rng = _resolve_rng(rng, "audit_events")
    now = _resolve_now(now)
    count = rng.randint(4, 8)
    events: list[dict[str, object]] = []
    for i in range(count):
        created = now - timedelta(hours=rng.randint(1, 72), minutes=rng.randint(0, 50))
        action = rng.choice(AUDIT_ACTIONS)
        company = rng.choice(COMPANY_NAMES)
        actor = f"{rng.choice(CONTACT_FIRST).lower()}@saberistic.com"
        entity_type: str | None = None
        entity_id: str | None = None
        summary_before: dict[str, object] | None = None
        summary_after: dict[str, object] | None = None
        if action == "pipeline.update":
            entity_type = "pipeline"
            entity_id = str(rng.randint(10, 99))
            summary_before = {"name": company}
            summary_after = {"pipeline_stage": rng.choice(list(PIPELINE_STAGES))}
        elif action == "research_record.create":
            entity_type = "research_record"
            entity_id = str(rng.randint(100, 999))
            summary_after = {
                "research_record_id": entity_id,
                "company_id": str(rng.randint(10, 99)),
                "record_type": rng.choice(["hypothesis", "verified_fact"]),
                "has_source_name": True,
                "has_source_url": True,
                "has_observed_value": True,
            }
        elif action == "pipeline_activity.create":
            entity_type = "pipeline_activity"
            entity_id = str(rng.randint(100, 999))
            summary_after = {
                "activity_id": entity_id,
                "company_id": str(rng.randint(10, 99)),
                "activity_type": "outreach",
                "created_at": created.isoformat(),
            }
        elif action == "company.create":
            entity_type = "company"
            entity_id = str(rng.randint(10, 99))
            summary_after = {
                "name": company,
                "domain": f"{company.lower().replace(' ', '-')}.example",
                "category": "fintech",
                "has_notes": True,
            }
        elif action == "company.archive":
            entity_type = "company"
            entity_id = str(rng.randint(10, 99))
            summary_before = {"name": company, "archived_at": None}
            summary_after = {
                "name": company,
                "archived_at": created.isoformat(),
            }
        elif action == "contact.create":
            entity_type = "contact"
            entity_id = str(rng.randint(100, 999))
            first = rng.choice(CONTACT_FIRST)
            last = rng.choice(CONTACT_LAST)
            summary_after = {
                "full_name": f"{first} {last}",
                "title": "CTO",
                "has_profile_url": True,
            }
        elif action == "contact.restore":
            entity_type = "contact"
            entity_id = str(rng.randint(100, 999))
            summary_before = {
                "full_name": f"{rng.choice(CONTACT_FIRST)} {rng.choice(CONTACT_LAST)}",
                "archived_at": (created - timedelta(days=3)).isoformat(),
            }
            summary_after = {
                "full_name": summary_before["full_name"],
                "archived_at": None,
            }
        elif "delete" in action:
            entity_type = "company"
            entity_id = str(rng.randint(10, 99))
            summary_after = {"ok": True}
        else:
            summary_after = {"ok": True}
        events.append(
            {
                "id": i + 1,
                "created_at": created,
                "actor": actor,
                "action": action,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "correlation_id": f"corr-preview-{rng.randint(1000, 9999)}",
                "summary_before": summary_before,
                "summary_after": summary_after,
            }
        )
    return events


@dataclass(frozen=True)
class PreviewLinkedInImportData:
    connection_count: int
    message_thread_count: int
    invitation_count: int
    company_follow_count: int
    duplicate_urls: tuple[str, ...]
    ignored_samples: tuple[str, ...]
    warnings: tuple[str, ...]


def build_preview_linkedin_import_data(
    *,
    rng: random.Random | None = None,
) -> PreviewLinkedInImportData:
    """Randomized LinkedIn import preview stats for ADMIN_PREVIEW_MODE."""
    rng = _resolve_rng(rng, "linkedin_import")
    return PreviewLinkedInImportData(
        connection_count=rng.randint(120, 840),
        message_thread_count=rng.randint(8, 64),
        invitation_count=rng.randint(3, 40),
        company_follow_count=rng.randint(5, 55),
        duplicate_urls=tuple(
            f"https://linkedin.com/in/{rng.choice(CONTACT_FIRST).lower()}-{rng.choice(CONTACT_LAST).lower()}"
            for _ in range(rng.randint(1, 3))
        ),
        ignored_samples=(
            "Logins.csv",
            "PhoneNumbers.csv",
            "Security_Challenges.csv",
            "Ads Clicked.csv",
        ),
        warnings=(
            "Connections.csv: 2 rows missing profile URL",
            "messages.csv: 1 row without conversation id",
        ),
    )


def render_preview_imports_main(
    *,
    rng: random.Random | None = None,
    now: datetime | None = None,
) -> str:
    """HTML main fragment for /admin/imports in preview mode (populated preview)."""
    now = _resolve_now(now)
    data = build_preview_linkedin_import_data(rng=rng)
    reconcile = build_preview_linkedin_reconcile(rng=rng)
    summary = reconcile["summary_counts"]
    assert isinstance(summary, dict)
    rows = reconcile["rows"]
    assert isinstance(rows, list)
    row_html = []
    for row in rows:
        assert isinstance(row, dict)
        identity = row.get("identity") or {}
        assert isinstance(identity, dict)
        label = html.escape(str(identity.get("full_name") or "Unknown"))
        outcome = html.escape(str(row.get("outcome")))
        tier = html.escape(str(row.get("match_tier") or "—"))
        row_html.append(
            f"<tr><td>{label}</td><td>{outcome}</td><td>{tier}</td>"
            f"<td>{html.escape(str(identity.get('company_name') or '—'))}</td></tr>"
        )
    reconcile_section = f"""
          <div class="linkedin-import-reconcile">
            <h2 class="admin-section-title" id="reconcile-title">LinkedIn reconcile preview</h2>
            <p class="admin-lede">
              Incremental merge preview — inserts, updates, unchanged rows, and conflicts.
              Connections absent from this export are preserved ({reconcile["absent_preserved"]} existing).
            </p>
            <dl class="linkedin-import-summary">
              <div><dt>Insert</dt><dd>{summary.get("insert", 0)}</dd></div>
              <div><dt>Update</dt><dd>{summary.get("update", 0)}</dd></div>
              <div><dt>Unchanged</dt><dd>{summary.get("unchanged", 0)}</dd></div>
              <div><dt>Conflict</dt><dd>{summary.get("conflict", 0)}</dd></div>
            </dl>
            <div class="admin-table-wrap">
              <table class="admin-table">
                <thead>
                  <tr>
                    <th scope="col">Connection</th>
                    <th scope="col">Outcome</th>
                    <th scope="col">Match tier</th>
                    <th scope="col">Company</th>
                  </tr>
                </thead>
                <tbody>
                  {"".join(row_html)}
                </tbody>
              </table>
            </div>
          </div>"""
    generated = now.strftime("%Y-%m-%d %H:%M:%S UTC")
    dup_rows = "".join(
        f"<li>{html.escape(url)}</li>" for url in data.duplicate_urls
    )
    ignored_rows = "".join(
        f"<li><code>{html.escape(name)}</code></li>" for name in data.ignored_samples
    )
    warning_rows = "".join(
        f"<li>{html.escape(warning)}</li>" for warning in data.warnings
    )
    return f"""        <section class="admin-section linkedin-import" aria-labelledby="imports-title">
          <p class="admin-preview-banner" role="status">Preview data — not production</p>
          <div class="admin-section-head">
            <div>
              <p class="admin-eyebrow">Data import</p>
              <h1 class="admin-title" id="imports-title">LinkedIn export preview</h1>
            </div>
          </div>
          <p class="admin-lede">
            Mock parsed export for screenshots
            (<time datetime="{html.escape(generated)}">{html.escape(generated)}</time>).
            Parsing runs locally; nothing is uploaded.
          </p>
          <div class="linkedin-import-privacy" role="note">
            <h2 class="admin-section-title">Privacy &amp; retention</h2>
            <dl class="linkedin-import-privacy-grid">
              <div><dt>Processed locally</dt><dd><code>Connections.csv</code>, <code>messages.csv</code>, <code>Invitations.csv</code>, <code>Company Follows.csv</code></dd></div>
              <div><dt>Ignored in archive</dt><dd>Logins, phones, security challenges, ads, receipts, and all other files</dd></div>
              <div><dt>Transmitted</dt><dd>Nothing in this preview step</dd></div>
              <div><dt>Retained</dt><dd>Nothing server-side until a future import-batch step</dd></div>
            </dl>
          </div>
          <div class="linkedin-import-status linkedin-import-status--ok" role="status">Preview ready — nothing uploaded.</div>
          <div class="linkedin-import-preview">
            <h2 class="admin-section-title">Import preview</h2>
            <dl class="admin-stat-row linkedin-import-stats">
              <div><dt>Connections</dt><dd>{data.connection_count}</dd></div>
              <div><dt>Message threads</dt><dd>{data.message_thread_count}</dd></div>
              <div><dt>Invitations</dt><dd>{data.invitation_count}</dd></div>
              <div><dt>Company follows</dt><dd>{data.company_follow_count}</dd></div>
            </dl>
            <h3 class="admin-section-title">Proposed changes (preview only)</h3>
            <ul class="linkedin-import-proposed-list">
              <li><strong>New connections:</strong> {data.connection_count}</li>
              <li><strong>Message threads:</strong> {data.message_thread_count}</li>
              <li><strong>Invitations:</strong> {data.invitation_count}</li>
              <li><strong>Company follows:</strong> {data.company_follow_count}</li>
            </ul>
            <h3 class="admin-section-title">Validation warnings</h3>
            <ul class="linkedin-import-warnings">{warning_rows}</ul>
            <h3 class="admin-section-title">Duplicate profile URLs in export</h3>
            <ul class="linkedin-import-duplicates">{dup_rows}</ul>
            <h3 class="admin-section-title">Recognized files</h3>
            <div class="admin-table-wrap">
              <table class="admin-table">
                <thead><tr><th>File</th><th>Rows</th><th>Valid</th><th>Skipped</th><th>Path in archive</th></tr></thead>
                <tbody>
                  <tr><td>connections.csv</td><td>{data.connection_count + 2}</td><td>{data.connection_count}</td><td>2</td><td>LinkedIn Export/Connections.csv</td></tr>
                  <tr><td>messages.csv</td><td>{data.message_thread_count * 4}</td><td>{data.message_thread_count * 4 - 1}</td><td>1</td><td>LinkedIn Export/messages.csv</td></tr>
                  <tr><td>Invitations.csv</td><td>{data.invitation_count}</td><td>{data.invitation_count}</td><td>0</td><td>LinkedIn Export/Invitations.csv</td></tr>
                  <tr><td>company follows.csv</td><td>{data.company_follow_count}</td><td>{data.company_follow_count}</td><td>0</td><td>LinkedIn Export/Company Follows.csv</td></tr>
                </tbody>
              </table>
            </div>
            <h3 class="admin-section-title">Ignored archive entries</h3>
            <ul class="linkedin-import-ignored">{ignored_rows}</ul>
          </div>
          {reconcile_section}
        </section>"""


PREVIEW_IMPORT_BATCH_IDS = (
    UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1"),
    UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa2"),
)


def build_preview_import_batches(
    *,
    rng: random.Random | None = None,
    now: datetime | None = None,
) -> tuple[list[dict[str, object]], int]:
    """Mock committed import batches for ADMIN_PREVIEW_MODE."""
    rng = _resolve_rng(rng, "import_batches")
    now = _resolve_now(now)
    batches: list[dict[str, object]] = []
    for index, batch_id in enumerate(PREVIEW_IMPORT_BATCH_IDS):
        created = now - timedelta(days=index + 1, hours=rng.randint(1, 8))
        batches.append(
            {
                "id": str(batch_id),
                "created_at": created,
                "updated_at": created,
                "source_type": "linkedin",
                "export_date": (created - timedelta(days=3)).date().isoformat(),
                "schema_version": "linkedin_export_v1",
                "checksum": f"preview-checksum-{index + 1:02d}{'a' * 52}",
                "actor": "preview-operator",
                "status": "committed" if index == 0 else "rolled_back",
                "summary_counts": {
                    "inserted": rng.randint(8, 24),
                    "updated": rng.randint(2, 9),
                    "unchanged": rng.randint(30, 90),
                    "skipped": rng.randint(0, 3),
                    "conflicted": 1 if index == 1 else 0,
                },
                "correlation_id": f"corr-preview-import-{index + 1}",
            }
        )
    return batches, len(batches)


def build_preview_import_batch_detail(
    batch_id: str,
    *,
    rng: random.Random | None = None,
    now: datetime | None = None,
) -> dict[str, object] | None:
    """Mock batch detail with representative row outcomes."""
    detail_rng = _resolve_rng(rng, f"import_batch_detail:{batch_id}")
    batches, _ = build_preview_import_batches(rng=rng, now=now)
    batch = next((item for item in batches if str(item["id"]) == batch_id), None)
    if batch is None:
        return None
    rows: list[dict[str, object]] = []
    outcomes = ("inserted", "updated", "unchanged", "skipped", "conflicted")
    for index, outcome in enumerate(outcomes):
        company = detail_rng.choice(COMPANY_NAMES)
        first = detail_rng.choice(CONTACT_FIRST)
        last = detail_rng.choice(CONTACT_LAST)
        rows.append(
            {
                "row_index": index,
                "source_kind": "linkedin_connection",
                "source_identity": {
                    "profile_url": f"https://linkedin.com/in/{first.lower()}-{last.lower()}",
                    "full_name": f"{first} {last}",
                    "company_name": company,
                    "title": detail_rng.choice(("CTO", "VP Engineering", "Founder")),
                },
                "outcome": outcome,
                "entity_type": "contact" if outcome != "skipped" else None,
                "entity_id": str(UUID(int=detail_rng.getrandbits(128), version=4))
                if outcome not in {"skipped", "conflicted"}
                else None,
                "detail": "Multiple contacts share this profile URL"
                if outcome == "conflicted"
                else ("Missing profile URL" if outcome == "skipped" else None),
            }
        )
    return {"batch": batch, "rows": rows}


def render_preview_section_main(
    *,
    label: str,
    summary: str,
    active_path: str,
    rng: random.Random | None = None,
    now: datetime | None = None,
) -> str:
    """HTML main fragment for an admin section page in preview mode."""
    now = _resolve_now(now)
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


def build_preview_icp_version() -> dict[str, object]:
    return {
        "id": UUID("99999999-9999-9999-9999-999999999901"),
        "version_number": 1,
        "label": "Default Saberistic ICP",
        "is_active": True,
        "created_by": "preview",
    }


def build_preview_icp_rules() -> list[dict[str, object]]:
    return [rule.model_dump() for rule in default_icp_rules()]


def build_preview_icp_score_rows(
    *,
    rng: random.Random | None = None,
    now: datetime | None = None,
) -> list[dict[str, object]]:
    score_rng = rng if rng is not None else _preview_rng("icp_score_rows")
    now = _resolve_now(now)
    rows: list[dict[str, object]] = []
    for index, company_id in enumerate(PREVIEW_PIPELINE_COMPANY_IDS):
        company = build_preview_company(company_id, now=now)
        rows.append(
            {
                "company_id": company_id,
                "company_name": company["name"],
                "total_score": round(score_rng.uniform(3.0, 9.5), 1),
                "computed_score": round(score_rng.uniform(3.0, 9.5), 1),
                "version_number": 1,
                "is_override": index == 1,
                "calculated_at": now - timedelta(days=index),
            }
        )
    return rows


def build_preview_icp_score_detail(
    company_id: UUID,
    *,
    rng: random.Random | None = None,
    now: datetime | None = None,
) -> dict[str, object] | None:
    _ = rng  # detail fixtures are deterministic; keep param for call-site compatibility
    now = _resolve_now(now)
    if company_id not in PREVIEW_PIPELINE_COMPANY_IDS:
        return None
    company = build_preview_company(company_id, now=now)
    rules = default_icp_rules()
    breakdown = []
    for index, rule in enumerate(rules):
        scored = index % 3 != 0
        breakdown.append(
            {
                "rule_id": rule.id,
                "dimension": rule.dimension,
                "label": rule.label,
                "weight": rule.weight,
                "points_awarded": rule.weight if scored else 0.0,
                "status": "scored" if scored else "missing_data",
                "evidence": (
                    [{"kind": "company_field", "field": "category", "value": "fintech"}]
                    if scored
                    else []
                ),
                "missing_inputs": [] if scored else ["company.stage"],
            }
        )
    is_override = company_id == PREVIEW_PIPELINE_COMPANY_IDS[1]
    computed_score = round(sum(item["points_awarded"] for item in breakdown), 1)
    total_score = 8.5 if is_override else computed_score
    return {
        "company": company,
        "active_version": build_preview_icp_version(),
        "snapshot": {
            "company_id": company_id,
            "version_number": 1,
            "total_score": total_score,
            "computed_score": computed_score,
            "breakdown": breakdown,
            "missing_inputs": ["company.stage", "research_records"],
            "calculated_at": now,
            "is_override": is_override,
            "override_reason": "Partner intro confirmed offline" if is_override else None,
            "override_by": "preview-operator" if is_override else None,
        },
    }


def build_preview_linkedin_reconcile(
    *,
    rng: random.Random | None = None,
) -> dict[str, object]:
    """Mock reconcile preview with insert, update, unchanged, and conflict rows."""
    rng = _resolve_rng(rng, "linkedin_reconcile")
    companies = list(COMPANY_NAMES)
    rng.shuffle(companies)
    rows: list[dict[str, object]] = [
        {
            "row_index": 0,
            "outcome": "insert",
            "identity": {
                "full_name": "Jordan Ellis",
                "profile_url": "https://linkedin.com/in/jordan-ellis",
                "company_name": companies[0],
                "title": "CTO",
            },
            "match_tier": "none",
            "field_changes": [{"field": "full_name", "before": None, "after": "Jordan Ellis"}],
        },
        {
            "row_index": 1,
            "outcome": "update",
            "identity": {
                "full_name": "Alex Nguyen",
                "profile_url": "https://linkedin.com/in/alex-nguyen",
                "company_name": companies[1],
                "title": "VP Engineering",
            },
            "match_tier": "profile_url",
            "contact_id": _preview_uuid(rng),
            "contact_label": "Alex Nguyen",
            "field_changes": [
                {"field": "title", "before": "Director of Engineering", "after": "VP Engineering"},
                {
                    "field": "company_id",
                    "before": None,
                    "after": str(UUID(int=rng.getrandbits(128), version=4)),
                },
            ],
        },
        {
            "row_index": 2,
            "outcome": "unchanged",
            "identity": {
                "full_name": "Sam Patel",
                "profile_url": "https://linkedin.com/in/sam-patel",
                "company_name": companies[2],
                "title": "Founder",
            },
            "match_tier": "profile_url",
            "contact_id": _preview_uuid(rng),
            "contact_label": "Sam Patel",
            "field_changes": [],
        },
        {
            "row_index": 3,
            "outcome": "conflict",
            "identity": {
                "full_name": "Riley Park",
                "profile_url": "https://linkedin.com/in/riley-park",
                "company_name": companies[3],
            },
            "match_tier": "name_company",
            "conflict_reason": "Multiple contacts match name and company",
            "conflict_candidates": [
                {
                    "contact_id": _preview_uuid(rng),
                    "full_name": "Riley Park",
                    "title": "COO",
                    "company_name": companies[3],
                    "profile_url": None,
                    "email": "riley@example.com",
                },
                {
                    "contact_id": _preview_uuid(rng),
                    "full_name": "Riley Park",
                    "title": "Advisor",
                    "company_name": companies[3],
                    "profile_url": "https://linkedin.com/in/riley-p",
                    "email": None,
                },
            ],
        },
    ]
    return {
        "rows": rows,
        "summary_counts": {
            "insert": 1,
            "update": 1,
            "unchanged": 1,
            "conflict": 1,
            "skipped": 0,
        },
        "absent_preserved": rng.randint(12, 48),
    }
