"""Ordered, idempotent SQL migrations for Render Postgres."""

from __future__ import annotations

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
        name="contact_buying_roles",
        up_sql="""
ALTER TABLE contacts ALTER COLUMN email DROP NOT NULL;

ALTER TABLE contacts ADD COLUMN IF NOT EXISTS title TEXT;
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS profile_url TEXT;
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS email_provenance TEXT;
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS email_permission TEXT;
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS last_interaction_at TIMESTAMPTZ;
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS relationship_strength INTEGER
    CHECK (
        relationship_strength IS NULL
        OR (relationship_strength >= 1 AND relationship_strength <= 5)
    );
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS notes TEXT;
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active'
    CHECK (status IN ('active', 'archived'));

CREATE INDEX IF NOT EXISTS idx_contacts_profile_url ON contacts (profile_url);
CREATE INDEX IF NOT EXISTS idx_contacts_status ON contacts (status);
CREATE INDEX IF NOT EXISTS idx_contacts_full_name ON contacts (full_name);

CREATE TABLE IF NOT EXISTS contact_buying_roles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    contact_id UUID NOT NULL REFERENCES contacts (id) ON DELETE CASCADE,
    role TEXT NOT NULL
        CHECK (role IN (
            'founder',
            'technical_buyer',
            'executive_buyer',
            'influencer',
            'investor',
            'introducer',
            'other'
        )),
    CONSTRAINT contact_buying_roles_unique UNIQUE (contact_id, role)
);

CREATE INDEX IF NOT EXISTS idx_contact_buying_roles_contact_id
    ON contact_buying_roles (contact_id);
""",
    ),

)
