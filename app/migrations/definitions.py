"""Ordered, idempotent SQL migrations for Render Postgres."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Migration:
    """Forward-only migration step."""

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
        version="004",
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
)
