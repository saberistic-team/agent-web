"""Ordered, idempotent SQL migrations for Render Postgres."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class Migration:
    """Forward-only migration step.

    Rollback strategy: migrations are additive and idempotent. There is no
    automatic down migration; reversing a change requires a new forward migration
    or a manual DBA restore. See docs/CRM_SCHEMA.md.
    """

    version: str
    name: str
    up_sql: str


def migration_content_digest(migration: Migration) -> str:
    """Stable SHA-256 of version, name, and SQL for immutability checks."""
    payload = f"{migration.version}\0{migration.name}\0{migration.up_sql}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# Digests for versions that must never be silently redefined (#210).
# When adding a new migration, leave prior entries unchanged. The CI job
# "Freeze shipped migrations" (scripts/freeze_shipped_migrations.py) freezes
# new versions after a healthy production deploy — do not hand-edit shipped digests.
FROZEN_MIGRATION_DIGESTS: dict[str, str] = {
    "001": "b25d23a80d13aca9fab1449d4ce7b50513b747a6bd6d00e234ea0ff21c0877f6",
    "002": "a74155b616b65ecb04f14cae1f2f33cf4e6a316d23c9452a6e4e3ac1161d6ed6",
    "003": "a110b52188674dea75d952f21f571da4b5d9da8ec4f0c7694cd84b4c7298c6dd",
    "004": "ca86d87534358f7c471241cb25ab1944dbe36fe749fc2c2212d47b2565c42545",
    "005": "959da61312e032d7f7a1a8ebd962d5e172f9c8e9a2406c56fe1924c4f0c91145",
    "006": "0792501de05c6c48f7cdf8613b87ad9a163fb9b624565558ab0144925d98cf3c",
    "007": "da82d83d66b0a4af2371a644394fdda464c5866cfa99ab9e8f71674315e4f760",
    "008": "375b45ea4df7ec8edc820be10507a63ea166d5a22767342cc94e243bb13ba91d",
    "009": "14d8f3e5f4f8e7080877ff60c0a86d377aed024218912e27fa3803eb9db9e33b",
    "010": "c748ec5abeb291273c31c719c32c4c0609b5c28d1e64b20be09ff2f1084ea99b",
    "011": "af4326258e2c3b005421f9894caf7059e75b46047581b4caa150d149fcfc906b",
    "012": "256322500ee7ac616de8f575a6b0a7c652c78924b9b1a3ca1007897626e88ef7",
    "013": "677757b25f70e5e1b8dea6aa244d458b276ddad8e751a837c9bceb84cd9b6308",
    "014": "9bb2a99e936e5ab77f75d1f94556715667cb97bff7dc185007ccdfe32f28f050",
    "015": "014080f78e50242cb2e5518567634f7522f844bd55d7c4dcba4c970df73d07b0",
}


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version="001",
        name="project_briefs",
        up_sql="""
CREATE TABLE IF NOT EXISTS project_briefs (
    id SERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    website TEXT NOT NULL,
    contact_method TEXT NOT NULL DEFAULT 'email'
        CHECK (contact_method IN ('email')),
    contact_value TEXT NOT NULL,
    brief TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending_payment'
        CHECK (status IN ('pending_payment', 'paid', 'abandoned')),
    stripe_session_id TEXT,
    stripe_payment_intent_id TEXT,
    paid_at TIMESTAMPTZ,
    utm_source TEXT,
    utm_medium TEXT,
    utm_campaign TEXT,
    utm_content TEXT,
    utm_term TEXT
);
""",
    ),
    Migration(
        version="002",
        name="project_briefs_utm_columns",
        up_sql="""
ALTER TABLE project_briefs ADD COLUMN IF NOT EXISTS utm_source TEXT;
ALTER TABLE project_briefs ADD COLUMN IF NOT EXISTS utm_medium TEXT;
ALTER TABLE project_briefs ADD COLUMN IF NOT EXISTS utm_campaign TEXT;
ALTER TABLE project_briefs ADD COLUMN IF NOT EXISTS utm_content TEXT;
ALTER TABLE project_briefs ADD COLUMN IF NOT EXISTS utm_term TEXT;
""",
    ),
    Migration(
        version="003",
        name="crm_foundation",
        up_sql="""
CREATE TABLE IF NOT EXISTS companies (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    name TEXT NOT NULL,
    website TEXT,
    status TEXT NOT NULL DEFAULT 'prospect'
        CHECK (status IN ('prospect', 'active', 'inactive'))
);

CREATE INDEX IF NOT EXISTS idx_companies_status ON companies (status);
CREATE INDEX IF NOT EXISTS idx_companies_website ON companies (website);

CREATE TABLE IF NOT EXISTS contacts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    company_id UUID REFERENCES companies (id) ON DELETE SET NULL,
    email TEXT NOT NULL,
    full_name TEXT,
    CONSTRAINT contacts_email_unique UNIQUE (email)
);

CREATE INDEX IF NOT EXISTS idx_contacts_company_id ON contacts (company_id);
CREATE INDEX IF NOT EXISTS idx_contacts_email ON contacts (email);

CREATE TABLE IF NOT EXISTS source_records (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source_type TEXT NOT NULL
        CHECK (source_type IN ('project_brief', 'manual', 'import', 'discovery')),
    external_id TEXT,
    company_id UUID REFERENCES companies (id) ON DELETE SET NULL,
    contact_id UUID REFERENCES contacts (id) ON DELETE SET NULL,
    payload JSONB,
    CONSTRAINT source_records_type_external_unique
        UNIQUE (source_type, external_id)
);

CREATE INDEX IF NOT EXISTS idx_source_records_company_id ON source_records (company_id);
CREATE INDEX IF NOT EXISTS idx_source_records_contact_id ON source_records (contact_id);
CREATE INDEX IF NOT EXISTS idx_source_records_source_type ON source_records (source_type);

CREATE TABLE IF NOT EXISTS activities (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    activity_type TEXT NOT NULL
        CHECK (activity_type IN (
            'note', 'email', 'call', 'meeting', 'status_change', 'payment'
        )),
    company_id UUID REFERENCES companies (id) ON DELETE CASCADE,
    contact_id UUID REFERENCES contacts (id) ON DELETE SET NULL,
    source_record_id UUID REFERENCES source_records (id) ON DELETE SET NULL,
    summary TEXT NOT NULL,
    metadata JSONB
);

CREATE INDEX IF NOT EXISTS idx_activities_company_id ON activities (company_id);
CREATE INDEX IF NOT EXISTS idx_activities_contact_id ON activities (contact_id);
CREATE INDEX IF NOT EXISTS idx_activities_source_record_id ON activities (source_record_id);
CREATE INDEX IF NOT EXISTS idx_activities_created_at ON activities (created_at);

CREATE TABLE IF NOT EXISTS admin_users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    email TEXT NOT NULL,
    display_name TEXT,
    role TEXT NOT NULL DEFAULT 'viewer'
        CHECK (role IN ('admin', 'editor', 'viewer')),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT admin_users_email_unique UNIQUE (email)
);

CREATE INDEX IF NOT EXISTS idx_admin_users_email ON admin_users (email);
CREATE INDEX IF NOT EXISTS idx_admin_users_is_active ON admin_users (is_active);
""",
    ),
    Migration(
        version="004",
        name="admin_sessions",
        up_sql="""
CREATE TABLE IF NOT EXISTS admin_sessions (
    id SERIAL PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    admin_username TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS admin_sessions_token_hash_idx ON admin_sessions (token_hash);
""",
    ),
    Migration(
        version="005",
        name="admin_login_rate_limits",
        up_sql="""
CREATE TABLE IF NOT EXISTS admin_login_rate_limits (
    limiter_key TEXT PRIMARY KEY,
    failure_count INTEGER NOT NULL DEFAULT 0,
    window_started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    locked_until TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS admin_login_rate_limits_locked_until_idx
    ON admin_login_rate_limits (locked_until);
CREATE INDEX IF NOT EXISTS admin_login_rate_limits_updated_at_idx
    ON admin_login_rate_limits (updated_at);
""",
    ),
    Migration(
        version="006",
        name="admin_csrf_binding",
        up_sql="""
CREATE TABLE IF NOT EXISTS admin_login_flows (
    id SERIAL PRIMARY KEY,
    flow_token_hash TEXT NOT NULL UNIQUE,
    csrf_token_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS admin_login_flows_flow_token_hash_idx
    ON admin_login_flows (flow_token_hash);

ALTER TABLE admin_sessions ADD COLUMN IF NOT EXISTS csrf_token_hash TEXT;
""",
    ),
    Migration(
        version="007",
        name="audit_events",
        up_sql="""
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS audit_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    entity_type TEXT,
    entity_id TEXT,
    correlation_id TEXT NOT NULL,
    summary_before JSONB,
    summary_after JSONB,
    metadata JSONB
);

CREATE INDEX IF NOT EXISTS idx_audit_events_created_at ON audit_events (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_events_action ON audit_events (action);
CREATE INDEX IF NOT EXISTS idx_audit_events_actor ON audit_events (actor);
CREATE INDEX IF NOT EXISTS idx_audit_events_correlation_id ON audit_events (correlation_id);

CREATE OR REPLACE FUNCTION prevent_audit_events_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'audit_events records are append-only';
END;
$$;

DROP TRIGGER IF EXISTS audit_events_no_update ON audit_events;
CREATE TRIGGER audit_events_no_update
    BEFORE UPDATE ON audit_events
    FOR EACH ROW
    EXECUTE FUNCTION prevent_audit_events_mutation();

DROP TRIGGER IF EXISTS audit_events_no_delete ON audit_events;
CREATE TRIGGER audit_events_no_delete
    BEFORE DELETE ON audit_events
    FOR EACH ROW
    EXECUTE FUNCTION prevent_audit_events_mutation();
""",
    ),
    Migration(
        version="008",
        name="research_records",
        up_sql="""
CREATE TABLE IF NOT EXISTS research_records (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    record_type TEXT NOT NULL
        CHECK (record_type IN (
            'verified_fact',
            'public_signal',
            'relationship_context',
            'hypothesis',
            'outreach_angle',
            'follow_up_note'
        )),
    company_id UUID NOT NULL REFERENCES companies (id) ON DELETE CASCADE,
    contact_id UUID REFERENCES contacts (id) ON DELETE SET NULL,
    body TEXT NOT NULL,
    source_name TEXT,
    source_url TEXT,
    observed_value TEXT,
    observed_at TIMESTAMPTZ,
    confidence NUMERIC(4, 3)
        CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    review_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ,
    metadata JSONB
);

CREATE INDEX IF NOT EXISTS idx_research_records_company_id
    ON research_records (company_id);
CREATE INDEX IF NOT EXISTS idx_research_records_contact_id
    ON research_records (contact_id);
CREATE INDEX IF NOT EXISTS idx_research_records_record_type
    ON research_records (record_type);
CREATE INDEX IF NOT EXISTS idx_research_records_expires_at
    ON research_records (expires_at);
CREATE INDEX IF NOT EXISTS idx_research_records_observed_at
    ON research_records (observed_at);
""",
    ),
    Migration(
        version="009",
        name="admin_login_flows_cleanup_indexes",
        up_sql="""
CREATE INDEX IF NOT EXISTS admin_login_flows_expires_at_idx
    ON admin_login_flows (expires_at)
    WHERE consumed_at IS NULL;
CREATE INDEX IF NOT EXISTS admin_login_flows_consumed_at_idx
    ON admin_login_flows (consumed_at)
    WHERE consumed_at IS NOT NULL;
""",
    ),
    Migration(
        version="010",
        name="company_records",
        up_sql="""
ALTER TABLE companies ADD COLUMN IF NOT EXISTS domain TEXT;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS category TEXT;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS stage TEXT;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS headcount_estimate INTEGER;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS funding_summary TEXT;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS target_status TEXT;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS last_verified_at DATE;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS notes TEXT;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_companies_domain ON companies (domain);
CREATE INDEX IF NOT EXISTS idx_companies_category ON companies (category);
CREATE INDEX IF NOT EXISTS idx_companies_stage ON companies (stage);
CREATE INDEX IF NOT EXISTS idx_companies_target_status ON companies (target_status);
CREATE INDEX IF NOT EXISTS idx_companies_archived_at ON companies (archived_at);
CREATE INDEX IF NOT EXISTS idx_companies_last_verified_at ON companies (last_verified_at);
""",
    ),
    Migration(
        version="011",
        name="acquisition_dashboard_indexes",
        up_sql="""
CREATE INDEX IF NOT EXISTS idx_research_records_review_at
    ON research_records (review_at)
    WHERE record_type = 'follow_up_note' AND review_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_research_records_created_at
    ON research_records (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_research_records_stale_evidence
    ON research_records (expires_at)
    WHERE record_type IN ('verified_fact', 'public_signal') AND expires_at IS NOT NULL;
""",
    ),
    Migration(
        version="012",
        name="contact_records",
        up_sql="""
ALTER TABLE contacts ALTER COLUMN email DROP NOT NULL;
ALTER TABLE contacts DROP CONSTRAINT IF EXISTS contacts_email_unique;

ALTER TABLE contacts ADD COLUMN IF NOT EXISTS title TEXT;
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS profile_url TEXT;
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS email_permission TEXT;
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS last_interaction_at DATE;
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS relationship_strength TEXT;
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS notes TEXT;
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS buying_roles TEXT[];
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ;

CREATE UNIQUE INDEX IF NOT EXISTS idx_contacts_email_unique
    ON contacts (LOWER(email))
    WHERE email IS NOT NULL AND archived_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_contacts_profile_url ON contacts (profile_url)
    WHERE profile_url IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_contacts_archived_at ON contacts (archived_at);
CREATE INDEX IF NOT EXISTS idx_contacts_last_interaction_at ON contacts (last_interaction_at);
CREATE INDEX IF NOT EXISTS idx_contacts_buying_roles ON contacts USING GIN (buying_roles);
""",
    ),
    Migration(
        version="013",
        name="acquisition_pipeline",
        up_sql="""
ALTER TABLE companies ADD COLUMN IF NOT EXISTS pipeline_stage TEXT;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS next_action TEXT;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS next_action_due_at TIMESTAMPTZ;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS pipeline_owner TEXT;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS expected_value_cents INTEGER;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS pipeline_loss_reason TEXT;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS pipeline_nurture_reason TEXT;

UPDATE companies SET pipeline_stage = 'researching' WHERE pipeline_stage IS NULL;
ALTER TABLE companies ALTER COLUMN pipeline_stage SET DEFAULT 'researching';
ALTER TABLE companies ALTER COLUMN pipeline_stage SET NOT NULL;

ALTER TABLE companies DROP CONSTRAINT IF EXISTS companies_pipeline_stage_check;
-- Stage keys must match app/pipeline_stages.py (PIPELINE_STAGE_ORDER).
ALTER TABLE companies ADD CONSTRAINT companies_pipeline_stage_check
    CHECK (pipeline_stage IN (
        'researching', 'qualified', 'ready_for_outreach', 'contacted', 'replied',
        'discovery_scheduled', 'diagnostic_proposed', 'diagnostic_paid',
        'larger_engagement', 'won', 'lost', 'nurture'
    ));

CREATE INDEX IF NOT EXISTS idx_companies_pipeline_stage
    ON companies (pipeline_stage)
    WHERE pipeline_stage IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_companies_next_action_due_at
    ON companies (next_action_due_at)
    WHERE next_action_due_at IS NOT NULL AND archived_at IS NULL;

CREATE TABLE IF NOT EXISTS pipeline_stage_history (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id UUID NOT NULL REFERENCES companies (id) ON DELETE CASCADE,
    from_stage TEXT,
    to_stage TEXT NOT NULL,
    changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    changed_by TEXT NOT NULL,
    metadata JSONB
);

CREATE INDEX IF NOT EXISTS idx_pipeline_stage_history_company_id
    ON pipeline_stage_history (company_id, changed_at DESC);

ALTER TABLE activities DROP CONSTRAINT IF EXISTS activities_activity_type_check;
ALTER TABLE activities ADD CONSTRAINT activities_activity_type_check
    CHECK (activity_type IN (
        'note', 'email', 'call', 'meeting', 'status_change', 'payment',
        'outreach', 'reply', 'proposal', 'task_completion'
    ));
""",
    ),


    Migration(
        version="014",
        name="import_batches",
        up_sql="""
CREATE TABLE IF NOT EXISTS import_batches (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source_type TEXT NOT NULL
        CHECK (source_type IN ('linkedin')),
    export_date DATE,
    schema_version TEXT NOT NULL,
    checksum TEXT NOT NULL,
    actor TEXT NOT NULL,
    status TEXT NOT NULL
        CHECK (status IN ('committed', 'failed', 'rolled_back')),
    summary_counts JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_message TEXT,
    correlation_id TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_import_batches_checksum_committed
    ON import_batches (checksum)
    WHERE status = 'committed';

CREATE INDEX IF NOT EXISTS idx_import_batches_created_at
    ON import_batches (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_import_batches_status
    ON import_batches (status);
CREATE INDEX IF NOT EXISTS idx_import_batches_actor
    ON import_batches (actor);

CREATE TABLE IF NOT EXISTS import_batch_rows (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    batch_id UUID NOT NULL REFERENCES import_batches (id) ON DELETE CASCADE,
    row_index INTEGER NOT NULL,
    source_kind TEXT NOT NULL,
    source_identity JSONB NOT NULL,
    outcome TEXT NOT NULL
        CHECK (outcome IN ('inserted', 'updated', 'unchanged', 'skipped', 'conflicted')),
    entity_type TEXT,
    entity_id UUID,
    prior_snapshot JSONB,
    applied_snapshot JSONB,
    detail TEXT,
    CONSTRAINT import_batch_rows_batch_index_unique UNIQUE (batch_id, row_index)
);

CREATE INDEX IF NOT EXISTS idx_import_batch_rows_batch_id
    ON import_batch_rows (batch_id);
CREATE INDEX IF NOT EXISTS idx_import_batch_rows_outcome
    ON import_batch_rows (outcome);
CREATE INDEX IF NOT EXISTS idx_import_batch_rows_entity_id
    ON import_batch_rows (entity_id)
    WHERE entity_id IS NOT NULL;
""",
    ),
    Migration(
        version="015",
        name="reconcile_acquisition_pipeline_schema",
        up_sql="""
-- Reconcile databases that applied the earlier incompatible form of migration
-- 013 (legacy owner/expected_value/stage_reason + company_stage_history) with
-- the canonical pipeline schema. Idempotent on fresh installs that already
-- applied the current 013. Legacy columns and company_stage_history are retained.

ALTER TABLE companies ADD COLUMN IF NOT EXISTS pipeline_owner TEXT;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS expected_value_cents INTEGER;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS pipeline_loss_reason TEXT;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS pipeline_nurture_reason TEXT;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'companies'
          AND column_name = 'owner'
    ) THEN
        UPDATE companies
        SET pipeline_owner = owner
        WHERE pipeline_owner IS NULL
          AND owner IS NOT NULL
          AND BTRIM(owner) <> '';
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'companies'
          AND column_name = 'expected_value'
    ) THEN
        UPDATE companies
        SET expected_value_cents = ROUND(expected_value * 100)::integer
        WHERE expected_value_cents IS NULL
          AND expected_value IS NOT NULL;
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'companies'
          AND column_name = 'stage_reason'
    ) THEN
        UPDATE companies
        SET pipeline_loss_reason = stage_reason
        WHERE pipeline_stage = 'lost'
          AND pipeline_loss_reason IS NULL
          AND stage_reason IS NOT NULL
          AND BTRIM(stage_reason) <> '';

        UPDATE companies
        SET pipeline_nurture_reason = stage_reason
        WHERE pipeline_stage = 'nurture'
          AND pipeline_nurture_reason IS NULL
          AND stage_reason IS NOT NULL
          AND BTRIM(stage_reason) <> '';
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS pipeline_stage_history (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id UUID NOT NULL REFERENCES companies (id) ON DELETE CASCADE,
    from_stage TEXT,
    to_stage TEXT NOT NULL,
    changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    changed_by TEXT NOT NULL,
    metadata JSONB
);

DO $$
BEGIN
    IF to_regclass('public.company_stage_history') IS NOT NULL THEN
        INSERT INTO pipeline_stage_history (
            id, company_id, from_stage, to_stage, changed_at, changed_by, metadata
        )
        SELECT
            h.id,
            h.company_id,
            h.from_stage,
            h.to_stage,
            h.changed_at,
            h.changed_by,
            CASE
                WHEN h.reason IS NULL OR BTRIM(h.reason) = '' THEN h.metadata
                WHEN h.metadata IS NULL THEN jsonb_build_object('legacy_reason', h.reason)
                ELSE h.metadata || jsonb_build_object('legacy_reason', h.reason)
            END
        FROM company_stage_history AS h
        ON CONFLICT (id) DO NOTHING;
    END IF;
END $$;

-- Rebuild named indexes so an earlier non-canonical definition cannot linger.
DROP INDEX IF EXISTS idx_companies_pipeline_stage;
CREATE INDEX IF NOT EXISTS idx_companies_pipeline_stage
    ON companies (pipeline_stage)
    WHERE pipeline_stage IS NOT NULL;

DROP INDEX IF EXISTS idx_companies_next_action_due_at;
CREATE INDEX IF NOT EXISTS idx_companies_next_action_due_at
    ON companies (next_action_due_at)
    WHERE next_action_due_at IS NOT NULL AND archived_at IS NULL;

DROP INDEX IF EXISTS idx_pipeline_stage_history_company_id;
CREATE INDEX IF NOT EXISTS idx_pipeline_stage_history_company_id
    ON pipeline_stage_history (company_id, changed_at DESC);
""",
    ),
    Migration(
        version="016",
        name="project_brief_payment_details",
        up_sql="""
ALTER TABLE project_briefs ADD COLUMN IF NOT EXISTS payment_subtotal_cents INTEGER;
ALTER TABLE project_briefs ADD COLUMN IF NOT EXISTS payment_discount_cents INTEGER;
ALTER TABLE project_briefs ADD COLUMN IF NOT EXISTS payment_amount_cents INTEGER;
ALTER TABLE project_briefs ADD COLUMN IF NOT EXISTS payment_currency TEXT;
ALTER TABLE project_briefs ADD COLUMN IF NOT EXISTS stripe_promotion_code_id TEXT;
ALTER TABLE project_briefs ADD COLUMN IF NOT EXISTS stripe_coupon_id TEXT;
""",
    ),
)
