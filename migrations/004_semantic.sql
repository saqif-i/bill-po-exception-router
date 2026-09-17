-- 004_semantic.sql
-- Owner: Volume 08. Applied as bpr_owner. Forward-only.
--
-- One row per invocation, and exactly one transition per row. The lifecycle is
-- deliberately multi-step: a remote call cannot be committed atomically with a
-- transaction, so the attempt is recorded BEFORE the call and finalised after.
-- An unrecorded call is therefore detectable.

BEGIN;

CREATE TABLE semantic_attempts (
    attempt_id            UUID        PRIMARY KEY,
    run_id                UUID        NOT NULL REFERENCES runs (run_id),
    correlation_id        UUID        NOT NULL,
    attempt_number        INTEGER     NOT NULL,
    status                TEXT        NOT NULL,

    -- Recorded with EVERY attempt, so a change in the evaluation numbers can be
    -- attributed to a prompt change, a model change, or neither.
    model_id              TEXT        NOT NULL,
    prompt_version        TEXT        NOT NULL,
    schema_version        TEXT        NOT NULL,

    -- The captured flag. A later configuration change cannot reinterpret a run
    -- that has already happened.
    semantic_review_enabled_at_attempt BOOLEAN NOT NULL,

    recommendation        TEXT        NULL,
    confidence            NUMERIC(4,3) NULL,
    explanation           TEXT        NULL,
    evidence              JSONB       NULL,
    rejection_reason      TEXT        NULL,
    rejection_detail      TEXT        NULL,

    started_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    finalised_at          TIMESTAMPTZ NULL,

    CONSTRAINT semantic_status_valid
        CHECK (status IN ('STARTED', 'SUCCEEDED', 'REJECTED')),

    CONSTRAINT semantic_recommendation_valid CHECK (
        recommendation IS NULL OR recommendation IN (
            'LIKELY_EQUIVALENT', 'LIKELY_DIFFERENT', 'INSUFFICIENT_EVIDENCE')),

    -- I01 and I02, in the schema rather than only in code. There is no column
    -- here for an outcome, a status, an amount or an action, so a recommendation
    -- has no route to one.
    CONSTRAINT semantic_success_is_complete CHECK (
        status <> 'SUCCEEDED'
        OR (recommendation IS NOT NULL AND confidence IS NOT NULL
            AND explanation IS NOT NULL AND evidence IS NOT NULL
            AND finalised_at IS NOT NULL)),

    CONSTRAINT semantic_rejection_has_a_reason CHECK (
        status <> 'REJECTED'
        OR (rejection_reason IS NOT NULL AND finalised_at IS NOT NULL)),

    CONSTRAINT semantic_started_is_empty CHECK (
        status <> 'STARTED'
        OR (recommendation IS NULL AND finalised_at IS NULL)),

    CONSTRAINT semantic_confidence_bounded CHECK (
        confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),

    CONSTRAINT semantic_explanation_bounded CHECK (
        explanation IS NULL OR length(explanation) <= 400),

    CONSTRAINT uq_semantic_attempt_number UNIQUE (run_id, attempt_number)
);

CREATE INDEX idx_semantic_run ON semantic_attempts (run_id, attempt_number);
CREATE INDEX idx_semantic_started
    ON semantic_attempts (started_at) WHERE status = 'STARTED';

-- At most ONE attempt may be in flight for a run. A second concurrent
-- invocation is prevented by the database rather than by worker discipline.
CREATE UNIQUE INDEX uq_semantic_one_in_flight
    ON semantic_attempts (run_id) WHERE status = 'STARTED';

GRANT SELECT, INSERT, UPDATE ON semantic_attempts TO bpr_app;
REVOKE DELETE ON semantic_attempts FROM bpr_app;

COMMIT;
