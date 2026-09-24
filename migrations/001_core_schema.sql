-- 001_core_schema.sql
-- Core schema. Applied as bpr_owner. Forward-only.
--
-- v1 scope note: a retention purge function is NOT
-- created here. v1 runs no retention process, so I20 is enforced structurally
-- by the table separation below and by bpr_app holding no DELETE on any
-- purgeable payload table. See docs/invariant-register-v1.md.
--
-- Table order is dependency order: seed_fixtures, runs, then everything that
-- references runs.

BEGIN;

-- ---------------------------------------------------------------------------
-- Runtime roles. The owner has CREATEROLE; the superuser credential is never
-- used outside the container bootstrap.
-- ---------------------------------------------------------------------------
DO $roles$
BEGIN
    -- Passwords arrive as session settings from the migration runner, never
    -- from this file. A login role without a password cannot authenticate, so
    -- creating one silently would leave a role that looks correct and is not.
    IF coalesce(current_setting('bpr.app_password', true), '') = ''
       OR coalesce(current_setting('bpr.retention_password', true), '') = '' THEN
        RAISE EXCEPTION
            'bpr.app_password and bpr.retention_password must be set before 001 runs';
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bpr_app') THEN
        EXECUTE format('CREATE ROLE bpr_app LOGIN PASSWORD %L',
                       current_setting('bpr.app_password', true));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bpr_retention') THEN
        EXECUTE format('CREATE ROLE bpr_retention LOGIN PASSWORD %L',
                       current_setting('bpr.retention_password', true));
    END IF;
END
$roles$;

GRANT CONNECT ON DATABASE bpr TO bpr_app;
GRANT USAGE   ON SCHEMA public TO bpr_app;
GRANT CONNECT ON DATABASE bpr TO bpr_retention;
GRANT USAGE   ON SCHEMA public TO bpr_retention;

-- No runtime role ever receives DDL authority.
REVOKE CREATE ON SCHEMA public FROM bpr_app, bpr_retention, PUBLIC;

-- ---------------------------------------------------------------------------
-- seed_fixtures. The allow-list. A record not active here never becomes a run
-- (I14). Structure only in v1; demo_seed populates it.
-- ---------------------------------------------------------------------------
CREATE TABLE seed_fixtures (
    fixture_id          UUID        PRIMARY KEY,
    seed_run_id         UUID        NOT NULL,
    fixture_name        TEXT        NOT NULL,
    fixture_reference   TEXT        NOT NULL,
    xero_resource_type  TEXT        NOT NULL,
    xero_resource_id    UUID        NOT NULL,
    human_reference     TEXT        NOT NULL,
    expected_scenario   TEXT        NOT NULL,
    disposition         TEXT        NOT NULL,
    fixture_status      TEXT        NOT NULL DEFAULT 'ACTIVE',
    created_at          TIMESTAMPTZ NOT NULL,
    imported_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT fixture_type_valid
        CHECK (xero_resource_type IN ('PURCHASE_ORDER','INVOICE')),
    CONSTRAINT fixture_reference_prefixed
        CHECK (fixture_reference LIKE 'BPR-SEED-%'),
    CONSTRAINT fixture_disposition_valid
        CHECK (disposition IN ('CREATED','REUSED')),
    CONSTRAINT fixture_status_valid
        CHECK (fixture_status IN ('ACTIVE','SUPERSEDED','RETIRED')),
    CONSTRAINT uq_fixture_run_reference UNIQUE (seed_run_id, fixture_reference),
    CONSTRAINT uq_fixture_resource      UNIQUE (xero_resource_id),
    CONSTRAINT uq_fixture_binding
        UNIQUE (fixture_id, xero_resource_id, xero_resource_type)
);

CREATE UNIQUE INDEX uq_fixture_reference_active
    ON seed_fixtures (fixture_reference) WHERE fixture_status = 'ACTIVE';
CREATE INDEX idx_fixture_active
    ON seed_fixtures (xero_resource_type, xero_resource_id)
    WHERE fixture_status = 'ACTIVE';

GRANT SELECT ON seed_fixtures TO bpr_app;
REVOKE INSERT, UPDATE, DELETE ON seed_fixtures FROM bpr_app;

-- ---------------------------------------------------------------------------
-- runs. Composite fixture binding: a run cannot point at a fixture whose
-- resource id or type disagrees.
-- ---------------------------------------------------------------------------
CREATE TABLE runs (
    run_id                  UUID        PRIMARY KEY,
    correlation_id          UUID        NOT NULL,
    seed_fixture_id         UUID        NOT NULL,
    seed_fixture_type       TEXT        NOT NULL DEFAULT 'INVOICE',
    xero_invoice_id         UUID        NOT NULL,
    xero_invoice_number     TEXT        NOT NULL,
    ingestion_version_key   TEXT        NOT NULL,
    xero_updated_date_utc   TIMESTAMPTZ NULL,
    bill_hash               TEXT        NOT NULL,
    po_hash                 TEXT        NULL,
    reconciliation_outcome  TEXT        NULL,
    workflow_status         TEXT        NOT NULL DEFAULT 'INGESTED',
    unprocessable_reason    TEXT        NULL,
    duplicate_invoice_key   TEXT        NULL,
    duplicate_business_key  TEXT        NULL,
    triage_destination      TEXT        NULL,
    human_review_reasons    TEXT[]      NOT NULL DEFAULT '{}',
    ingested_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT runs_fixture_is_invoice CHECK (seed_fixture_type = 'INVOICE'),

    CONSTRAINT runs_fixture_binding
        FOREIGN KEY (seed_fixture_id, xero_invoice_id, seed_fixture_type)
        REFERENCES seed_fixtures (fixture_id, xero_resource_id, xero_resource_type),

    CONSTRAINT runs_outcome_valid CHECK (
        reconciliation_outcome IS NULL
        OR reconciliation_outcome IN ('MATCHED','REVIEW_REQUIRED','UNPROCESSABLE')),

    CONSTRAINT runs_status_valid CHECK (workflow_status IN (
        'INGESTED','RECONCILING','REVIEW_READY','NOTIFY_PENDING','AWAITING_TRIAGE',
        'TRIAGED','ACTION_PENDING','COMPLETED','ACTION_FAILED')),

    -- I28: a run is never observable with an outcome set while still reconciling
    CONSTRAINT runs_pre_reconciliation_has_no_outcome CHECK (
        workflow_status NOT IN ('INGESTED','RECONCILING')
        OR reconciliation_outcome IS NULL),

    CONSTRAINT runs_review_states_require_review CHECK (
        workflow_status NOT IN ('REVIEW_READY','NOTIFY_PENDING','AWAITING_TRIAGE',
                                'TRIAGED','ACTION_PENDING','ACTION_FAILED')
        OR reconciliation_outcome IS NOT DISTINCT FROM 'REVIEW_REQUIRED'),

    CONSTRAINT runs_completed_has_outcome CHECK (
        workflow_status <> 'COMPLETED' OR reconciliation_outcome IS NOT NULL),

    CONSTRAINT runs_unprocessable_has_reason CHECK (
        reconciliation_outcome <> 'UNPROCESSABLE' OR unprocessable_reason IS NOT NULL),

    CONSTRAINT uq_runs_identity UNIQUE (run_id, xero_invoice_id)
);

CREATE UNIQUE INDEX uq_runs_ingestion_version
    ON runs (xero_invoice_id, ingestion_version_key);
CREATE INDEX idx_runs_status    ON runs (workflow_status, ingested_at);
CREATE INDEX idx_runs_retention ON runs (workflow_status, run_id);
CREATE INDEX idx_runs_dupinv ON runs (duplicate_invoice_key)  WHERE duplicate_invoice_key  IS NOT NULL;
CREATE INDEX idx_runs_dupbiz ON runs (duplicate_business_key) WHERE duplicate_business_key IS NOT NULL;

-- The composite FK proves the binding; this proves the fixture is still active.
CREATE OR REPLACE FUNCTION runs_require_active_fixture()
RETURNS TRIGGER AS $fn$
DECLARE fixture_state TEXT;
BEGIN
    SELECT fixture_status INTO fixture_state
      FROM seed_fixtures WHERE fixture_id = NEW.seed_fixture_id;
    IF fixture_state IS DISTINCT FROM 'ACTIVE' THEN
        RAISE EXCEPTION 'run % references fixture % with status %, expected ACTIVE',
            NEW.run_id, NEW.seed_fixture_id, COALESCE(fixture_state, 'MISSING');
    END IF;
    RETURN NEW;
END;
$fn$ LANGUAGE plpgsql;

CREATE TRIGGER trg_runs_require_active_fixture
BEFORE INSERT ON runs
FOR EACH ROW EXECUTE FUNCTION runs_require_active_fixture();

GRANT SELECT, INSERT, UPDATE ON runs TO bpr_app;
REVOKE DELETE ON runs FROM bpr_app;

-- ---------------------------------------------------------------------------
-- run_snapshots. Purgeable content, separate table from the immutable run
-- metadata above (I20).
-- ---------------------------------------------------------------------------
CREATE TABLE run_snapshots (
    run_id                     UUID        PRIMARY KEY REFERENCES runs (run_id),
    bill_snapshot              JSONB       NULL,
    po_snapshot                JSONB       NULL,
    account_reference_snapshot JSONB       NULL,
    expires_at                 TIMESTAMPTZ NOT NULL,
    created_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT run_snapshot_minimum_retention
        CHECK (expires_at >= created_at + INTERVAL '30 days')
);
CREATE INDEX idx_run_snapshots_expiry ON run_snapshots (expires_at, run_id);

GRANT SELECT, INSERT, UPDATE ON run_snapshots TO bpr_app;
REVOKE DELETE ON run_snapshots FROM bpr_app;
REVOKE ALL    ON run_snapshots FROM bpr_retention;

-- ---------------------------------------------------------------------------
-- poll_cursors. The polling high-water mark.
-- ---------------------------------------------------------------------------
CREATE TABLE poll_cursors (
    cursor_name          TEXT        PRIMARY KEY,
    high_water_mark      TIMESTAMPTZ NOT NULL,
    last_poll_started_at TIMESTAMPTZ NULL,
    last_poll_ended_at   TIMESTAMPTZ NULL,
    last_poll_status     TEXT        NULL,
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

GRANT SELECT, INSERT, UPDATE ON poll_cursors TO bpr_app;
REVOKE DELETE ON poll_cursors FROM bpr_app;

-- ---------------------------------------------------------------------------
-- integration_events. Append-only, enforced by trigger AND by privilege.
-- ---------------------------------------------------------------------------
CREATE TABLE integration_events (
    event_id        UUID        PRIMARY KEY,
    correlation_id  UUID        NOT NULL,
    run_id          UUID        NULL REFERENCES runs (run_id),
    subject_id      UUID        NULL,
    event_type      TEXT        NOT NULL,
    event_status    TEXT        NOT NULL,
    source          TEXT        NOT NULL,
    payload_hash    TEXT        NULL,
    occurred_at     TIMESTAMPTZ NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT events_source_valid CHECK (source IN (
        'POLLER','RECONCILER','SEMANTIC','NOTIFIER','SLACK_INBOUND',
        'OUTBOX_WORKER','WEBHOOK_INGRESS','RETENTION','SEED','ADMIN'))
);

CREATE INDEX idx_events_correlation ON integration_events (correlation_id, occurred_at);
CREATE INDEX idx_events_run         ON integration_events (run_id, occurred_at);
CREATE INDEX idx_events_subject     ON integration_events (subject_id, occurred_at);
CREATE INDEX idx_events_type_time   ON integration_events (event_type, occurred_at);

CREATE OR REPLACE FUNCTION integration_events_append_only()
RETURNS TRIGGER AS $fn$
BEGIN
    RAISE EXCEPTION 'integration_events is append-only: % is not permitted', TG_OP;
END;
$fn$ LANGUAGE plpgsql;

CREATE TRIGGER trg_integration_events_append_only
BEFORE UPDATE OR DELETE ON integration_events
FOR EACH ROW EXECUTE FUNCTION integration_events_append_only();

REVOKE UPDATE, DELETE ON public.integration_events FROM bpr_app;
GRANT  INSERT, SELECT  ON public.integration_events TO   bpr_app;
REVOKE ALL             ON public.integration_events FROM bpr_retention;

-- ---------------------------------------------------------------------------
-- integration_event_payloads. Purgeable content, separate from the immutable
-- event metadata above.
-- ---------------------------------------------------------------------------
CREATE TABLE integration_event_payloads (
    event_id    UUID        PRIMARY KEY REFERENCES integration_events (event_id),
    payload     JSONB       NOT NULL,
    expires_at  TIMESTAMPTZ NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT event_payload_minimum_retention
        CHECK (expires_at >= created_at + INTERVAL '30 days')
);

CREATE INDEX idx_event_payloads_expiry ON integration_event_payloads (expires_at, event_id);

GRANT INSERT, SELECT ON public.integration_event_payloads TO bpr_app;
REVOKE DELETE        ON public.integration_event_payloads FROM bpr_app;
REVOKE ALL           ON public.integration_event_payloads FROM bpr_retention;

-- ---------------------------------------------------------------------------
-- idempotency_registry. Every externally triggered operation lands here (I36).
-- ---------------------------------------------------------------------------
CREATE TABLE idempotency_registry (
    ledger_id        UUID        PRIMARY KEY,
    scope            TEXT        NOT NULL,
    idempotency_key  TEXT        NOT NULL,
    correlation_id   UUID        NOT NULL,
    request_hash     TEXT        NOT NULL,
    status           TEXT        NOT NULL,
    error_class      TEXT        NULL,
    owner_id         TEXT        NULL,
    claim_generation BIGINT      NOT NULL DEFAULT 0,
    locked_at        TIMESTAMPTZ NULL,
    lease_expires_at TIMESTAMPTZ NULL,
    attempt_count    INTEGER     NOT NULL DEFAULT 1,
    result_ref       TEXT        NULL,
    result_status    INTEGER     NULL,
    result_summary   JSONB       NULL,
    error_code       TEXT        NULL,
    retained_until   TIMESTAMPTZ NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at     TIMESTAMPTZ NULL,

    CONSTRAINT idem_status_valid
        CHECK (status IN ('PROCESSING','SUCCEEDED','FAILED')),
    CONSTRAINT idem_error_class_valid
        CHECK (error_class IS NULL OR error_class IN ('RETRYABLE','PERMANENT')),
    CONSTRAINT idem_scope_bounded CHECK (length(scope) BETWEEN 1 AND 256),
    CONSTRAINT idem_key_valid CHECK (
        length(idempotency_key) BETWEEN 16 AND 128
        AND idempotency_key ~ '^[A-Za-z0-9][A-Za-z0-9._:-]*$'),
    CONSTRAINT idem_request_hash_valid CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT idem_lease_coherent CHECK (
        (status = 'PROCESSING' AND owner_id IS NOT NULL
                               AND locked_at IS NOT NULL
                               AND lease_expires_at IS NOT NULL)
        OR (status <> 'PROCESSING' AND owner_id IS NULL
                                   AND locked_at IS NULL
                                   AND lease_expires_at IS NULL)),
    CONSTRAINT idem_terminal_coherent CHECK (
        (status IN ('SUCCEEDED','FAILED') AND completed_at IS NOT NULL
             AND retained_until IS NOT NULL
             AND retained_until >= completed_at + INTERVAL '90 days')
        OR (status = 'PROCESSING' AND completed_at IS NULL
                                  AND retained_until IS NULL)),
    CONSTRAINT idem_success_has_result CHECK (
        status <> 'SUCCEEDED' OR (result_summary IS NOT NULL AND result_status IS NOT NULL)),
    CONSTRAINT idem_summary_bounded CHECK (
        result_summary IS NULL OR length(result_summary::text) <= 4096),
    CONSTRAINT idem_failed_has_class CHECK (
        status <> 'FAILED' OR error_class IS NOT NULL)
);

CREATE UNIQUE INDEX uq_idempotency ON idempotency_registry (scope, idempotency_key);
CREATE INDEX idx_idempotency_leases
    ON idempotency_registry (lease_expires_at) WHERE status = 'PROCESSING';

GRANT SELECT, INSERT, UPDATE ON idempotency_registry TO bpr_app;
REVOKE DELETE ON idempotency_registry FROM bpr_app;

-- ---------------------------------------------------------------------------
-- The migration ledger is created by the runner before this file is applied.
-- Readiness reports MIGRATIONS_NOT_READY by reading it, so the runtime role
-- needs SELECT. It never needs anything more.
-- ---------------------------------------------------------------------------
GRANT SELECT ON schema_migrations TO bpr_app;
REVOKE INSERT, UPDATE, DELETE ON schema_migrations FROM bpr_app;

COMMIT;
