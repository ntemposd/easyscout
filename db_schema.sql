-- Complete Database Schema for Easyscout
-- PostgreSQL 17+ required
-- Safe to run multiple times; uses IF NOT EXISTS throughout.

-- =============================================================================
-- CORE TABLES (Credits & Reports)
-- =============================================================================

-- Credit ledger (transaction log)
CREATE TABLE IF NOT EXISTS public.credit_ledger (
    id BIGSERIAL PRIMARY KEY,
    user_id UUID NOT NULL,
    delta INTEGER NOT NULL,
    reason TEXT NOT NULL,
    source_type TEXT,
    source_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Ensure idempotent ledger writes keyed by (source_type, source_id)
CREATE UNIQUE INDEX IF NOT EXISTS credit_ledger_source_type_id_uidx
ON public.credit_ledger(source_type, source_id)
WHERE source_type IS NOT NULL AND source_id IS NOT NULL;

-- User credits (account balances)
CREATE TABLE IF NOT EXISTS public.user_credits (
    user_id UUID PRIMARY KEY,
    balance INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Reports (generated player scouting reports)
CREATE TABLE IF NOT EXISTS public.reports (
    id BIGSERIAL PRIMARY KEY,
    user_id UUID NOT NULL,
    player_name TEXT NOT NULL,
    query TEXT,
    report_md TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    payload JSONB,
    cached BOOLEAN NOT NULL DEFAULT FALSE,
    query_key TEXT,
    UNIQUE(user_id, query_key)
);

-- Report indexes
CREATE INDEX IF NOT EXISTS idx_reports_user_id ON public.reports(user_id);
CREATE INDEX IF NOT EXISTS idx_reports_created_at ON public.reports(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_reports_updated_at ON public.reports(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_reports_player_name ON public.reports(player_name);
CREATE INDEX IF NOT EXISTS idx_reports_payload_gin ON public.reports USING gin(payload jsonb_path_ops);
CREATE INDEX IF NOT EXISTS idx_reports_player_name_lower ON public.reports(LOWER(player_name));
CREATE INDEX IF NOT EXISTS idx_reports_query_key ON public.reports(query_key);

-- =============================================================================
-- COST TRACKING (Token Usage & LLM Monitoring)
-- =============================================================================

CREATE TABLE IF NOT EXISTS public.cost_tracking (
    id SERIAL PRIMARY KEY,
    user_id TEXT NOT NULL,
    report_id INTEGER NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    estimated_cost NUMERIC(10, 6) NOT NULL DEFAULT 0.0,
    player_name TEXT DEFAULT '',
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_cost_tracking_user_id ON public.cost_tracking(user_id);
CREATE INDEX IF NOT EXISTS idx_cost_tracking_report_id ON public.cost_tracking(report_id);
CREATE INDEX IF NOT EXISTS idx_cost_tracking_timestamp ON public.cost_tracking(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_cost_tracking_model ON public.cost_tracking(model);

-- =============================================================================
-- SEMANTIC SEARCH (Embeddings for Fuzzy Matching)
-- =============================================================================

CREATE TABLE IF NOT EXISTS public.report_embeddings (
    report_id INTEGER PRIMARY KEY,
    embedding_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_embeddings_report_id ON public.report_embeddings(report_id);

CREATE TABLE IF NOT EXISTS public.query_embeddings (
    query_hash TEXT PRIMARY KEY,
    query_text TEXT NOT NULL,
    embedding_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_query_embeddings_hash ON public.query_embeddings(query_hash);

-- =============================================================================
-- INSTRUMENTATION (Metrics & Performance Monitoring)
-- =============================================================================

CREATE TABLE IF NOT EXISTS public.metrics (
    name TEXT PRIMARY KEY,
    count INTEGER NOT NULL DEFAULT 0,
    first_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_metrics_name ON public.metrics(name);

CREATE TABLE IF NOT EXISTS public.timings (
    name TEXT PRIMARY KEY,
    count INTEGER NOT NULL DEFAULT 0,
    total_ms DOUBLE PRECISION NOT NULL DEFAULT 0,
    last_ms DOUBLE PRECISION NOT NULL DEFAULT 0,
    first_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_timings_name ON public.timings(name);

CREATE TABLE IF NOT EXISTS public.timing_samples (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    ms DOUBLE PRECISION NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_timing_samples_name ON public.timing_samples(name);

-- =============================================================================
-- NOTES
-- =============================================================================
-- 
-- Stripe payment tables (stripe_events, stripe_purchases) are intentionally
-- excluded from this schema. For local development, use the DEV_TOOLS=1 mode
-- to grant credits via /api/dev/grant_credits endpoint.
--
-- This allows contributors to test the core scouting functionality without
-- needing Stripe integration or production payment infrastructure.
--
