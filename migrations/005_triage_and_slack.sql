-- 005_triage_and_slack.sql
-- Owner: Volume 09. Applied as bpr_owner. Forward-only.
--
-- Reduced for this build: the durable SLACK_RESULT_UPDATE lifecycle belongs to
-- the transactional outbox, which is not built here. The card update is
-- best-effort and is recorded, not queued. See ADR-007.

BEGIN;

-- ---------------------------------------------------------------------------
-- slack_notifications. One card per run, enforced.
--
-- At-least-once posting is the honest position without a durable outbox: a post
-- that succeeds with a lost response can produce a second card. That is visible
-- here rather than hidden, and it is never claimed to be exactly-once.
-- ---------------------------------------------------------------------------
CREATE TABLE slack_notifications (
    notification_id  UUID        PRIMARY KEY,
    run_id           UUID        NOT NULL REFERENCES runs (run_id),
    correlation_id   UUID        NOT NULL,
    channel          TEXT        NOT NULL,
    destination      TEXT        NOT NULL,
    message_ts       TEXT        NULL,
    post_status      TEXT        NOT NULL,
    post_error       TEXT        NULL,
    card_updated_at  TIMESTAMPTZ NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT slack_post_status_valid
        CHECK (post_status IN ('POSTED', 'FAILED', 'POSSIBLE_DUPLICATE')),
    CONSTRAINT slack_destination_valid
        CHECK (destination IN ('FINANCE', 'PROCUREMENT', 'AP_REVIEW', 'DUPLICATE_REVIEW')),
    CONSTRAINT slack_posted_has_a_timestamp
        CHECK (post_status <> 'POSTED' OR message_ts IS NOT NULL),
    CONSTRAINT uq_slack_one_card_per_run UNIQUE (run_id)
);

CREATE INDEX idx_slack_message ON slack_notifications (message_ts);

-- ---------------------------------------------------------------------------
-- triage_decisions. Immutable, one per run.
--
-- Records WHAT THE PERSON WAS SHOWN at the time, not merely what they chose.
-- Six weeks later "why was this allowed through" is answerable, which is the
-- whole reason the manual process needed replacing.
-- ---------------------------------------------------------------------------
CREATE TABLE triage_decisions (
    decision_id        UUID        PRIMARY KEY,
    run_id             UUID        NOT NULL REFERENCES runs (run_id),
    correlation_id     UUID        NOT NULL,
    action             TEXT        NOT NULL,
    decided_by         TEXT        NOT NULL,
    decided_by_name    TEXT        NULL,
    decided_at         TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- The evidence shown on the card when the button was pressed.
    shown_exception_codes   TEXT[]  NOT NULL,
    shown_destination       TEXT    NOT NULL,
    shown_recommendation    TEXT    NULL,
    shown_confidence        NUMERIC(4,3) NULL,
    shown_gate_reason       TEXT    NOT NULL,

    slack_message_ts   TEXT        NULL,
    slack_interaction_id TEXT      NOT NULL,

    -- I03. No control is named Approve or Reject, because none of them is one.
    CONSTRAINT triage_action_valid CHECK (action IN (
        'MARK_REVIEWED', 'SEND_TO_FINANCE', 'SEND_TO_PROCUREMENT',
        'REQUEST_MORE_INFORMATION', 'ESCALATE', 'CLOSE_AS_DUPLICATE')),

    CONSTRAINT triage_confidence_bounded CHECK (
        shown_confidence IS NULL
        OR (shown_confidence >= 0 AND shown_confidence <= 1)),

    -- One decision per run. A second click on the same card is refused by the
    -- database rather than by a check the application might skip.
    CONSTRAINT uq_triage_one_per_run UNIQUE (run_id),

    -- Slack retries a delivery it believes failed. The same interaction landing
    -- twice must not produce two decisions.
    CONSTRAINT uq_triage_interaction UNIQUE (slack_interaction_id)
);

CREATE INDEX idx_triage_decided ON triage_decisions (decided_at);

CREATE OR REPLACE FUNCTION triage_decisions_immutable()
RETURNS TRIGGER AS $fn$
BEGIN
    RAISE EXCEPTION 'triage_decisions is immutable: % is not permitted', TG_OP;
END;
$fn$ LANGUAGE plpgsql;

CREATE TRIGGER trg_triage_immutable
BEFORE UPDATE OR DELETE ON triage_decisions
FOR EACH ROW EXECUTE FUNCTION triage_decisions_immutable();

GRANT SELECT, INSERT, UPDATE ON slack_notifications TO bpr_app;
GRANT SELECT, INSERT         ON triage_decisions   TO bpr_app;
REVOKE UPDATE, DELETE ON triage_decisions   FROM bpr_app;
REVOKE DELETE          ON slack_notifications FROM bpr_app;

COMMIT;
