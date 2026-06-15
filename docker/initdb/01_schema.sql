-- Harness Postgres schema (worker-mode dispatch + optional decision sink + demo data).
--
-- This is the local stand-in for the slice of the Verity governance DB the
-- worker touches. In production the worker NEVER connects to this directly
-- (ADR-0003 API-only); here, the SKIP LOCKED queue IS the dispatch stub
-- (ADR-0015 VERITY_DISPATCH_MODE=postgres fallback).

-- ── dispatch queue (worker mode) ──
CREATE TABLE IF NOT EXISTS execution_run (
    id              UUID PRIMARY KEY,
    entity_kind     TEXT NOT NULL,            -- 'task' | 'agent'
    entity_name     TEXT NOT NULL,
    channel         TEXT NOT NULL DEFAULT 'production',
    input_json      JSONB NOT NULL,
    status          TEXT NOT NULL DEFAULT 'queued',  -- queued|executing|suspended|complete|failed|max_turns
    attempt         INT  NOT NULL DEFAULT 0,
    worker_id       TEXT,
    suspension_id   UUID,                     -- set when status='suspended'
    decision_log_id UUID,
    output_json     JSONB,
    error_message   TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    claimed_at      TIMESTAMPTZ,
    finished_at     TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_execution_run_claim
    ON execution_run (status, created_at);

-- ── decision sink (only used when DECISION_SINK=postgres) ──
-- Shape mirrors Verity's agent_decision_log (flat scalars + nested JSONB).
CREATE TABLE IF NOT EXISTS agent_decision_log (
    id                  UUID PRIMARY KEY,
    entity_kind         TEXT NOT NULL,
    entity_name         TEXT NOT NULL,
    entity_version      TEXT NOT NULL,
    channel             TEXT NOT NULL,
    mock_mode           BOOLEAN NOT NULL DEFAULT false,
    parent_decision_id  UUID,
    decision_depth      INT  NOT NULL DEFAULT 0,
    step_name           TEXT,
    input_json          JSONB,
    output_json         JSONB,
    reasoning_text      TEXT,
    models_used         JSONB,
    input_tokens        INT  NOT NULL DEFAULT 0,
    output_tokens       INT  NOT NULL DEFAULT 0,
    duration_ms         INT  NOT NULL DEFAULT 0,
    message_history     JSONB,
    tool_calls_made     JSONB,
    source_resolutions  JSONB,
    target_writes       JSONB,
    application         TEXT NOT NULL DEFAULT 'harness',
    status              TEXT NOT NULL,
    error_message       TEXT,
    hitl_required       BOOLEAN NOT NULL DEFAULT false,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_decision_parent ON agent_decision_log (parent_decision_id);

-- ── demo data the underwriting_agent's Postgres SOURCE reads ──
CREATE TABLE IF NOT EXISTS submission (
    id               INT PRIMARY KEY,
    named_insured    TEXT    NOT NULL,
    line_of_business TEXT    NOT NULL,
    state            TEXT    NOT NULL,
    address          TEXT    NOT NULL,
    occupancy        TEXT    NOT NULL,
    construction     TEXT    NOT NULL,
    year_built       INT,
    square_feet      INT,
    protection_class INT,
    sprinklered      BOOLEAN NOT NULL DEFAULT false,
    tiv              NUMERIC NOT NULL,
    coverage_limit   NUMERIC NOT NULL,
    deductible       INT     NOT NULL,
    valuation        TEXT    NOT NULL DEFAULT 'replacement_cost',
    coinsurance      NUMERIC NOT NULL DEFAULT 0.9
);
INSERT INTO submission VALUES
  (1, 'Lakeview Bistro LLC',  'commercial_property', 'IL',
   '221 Lakeview Ave, Chicago, IL',
   'restaurant', 'joisted_masonry', 1998, 4200,  4, true,
   1850000, 1850000, 5000,  'replacement_cost', 0.9),
  (2, 'Gulfside Storage Inc', 'commercial_property', 'FL',
   '9 Harbor Rd, Tampa, FL',
   'warehouse',  'frame',            1975, 60000, 8, false,
   8000000, 8000000, 10000, 'replacement_cost', 0.9)
ON CONFLICT (id) DO NOTHING;
