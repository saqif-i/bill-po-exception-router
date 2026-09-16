-- 003_reconciliation.sql
-- Owner: Volume 06. Applied as bpr_owner. Forward-only.
--
-- The exception_code CHECK below is generated from
-- policy_service/domain/enums.ExceptionCode, so the database and the engine
-- cannot drift. A test regenerates it and asserts the file matches.

BEGIN;

-- ---------------------------------------------------------------------------
-- reconciliation_results. ONE per run, enforced.
--
-- Reconciliation is deterministic over an immutable snapshot, so re-running it
-- must produce the same answer. A genuine change in Xero produces a new
-- ingestion_version_key and therefore a new run, not a second result.
-- ---------------------------------------------------------------------------
CREATE TABLE reconciliation_results (
    result_id                 UUID        PRIMARY KEY,
    run_id                    UUID        NOT NULL REFERENCES runs (run_id),
    correlation_id            UUID        NOT NULL,
    reconciliation_outcome    TEXT        NOT NULL,
    tolerance_version         TEXT        NOT NULL,
    account_reference_version TEXT        NULL,
    account_reference_hash    TEXT        NULL,
    po_number                 TEXT        NULL,
    xero_po_id                UUID        NULL,
    paired_line_count         INTEGER     NOT NULL,
    unpaired_bill_lines       INTEGER     NOT NULL,
    unpaired_po_lines         INTEGER     NOT NULL,
    semantic_permitted        BOOLEAN     NOT NULL DEFAULT FALSE,
    semantic_gate_reason      TEXT        NOT NULL,
    line_comparisons          JSONB       NOT NULL,
    residual_comparison       JSONB       NULL,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT recon_outcome_valid
        CHECK (reconciliation_outcome IN ('MATCHED', 'REVIEW_REQUIRED', 'UNPROCESSABLE')),

    -- Line reconciliation was reached, so the validated chart snapshot that
    -- was used must be reproducible after run_snapshots is purged.
    CONSTRAINT recon_account_reference_coherent CHECK (
        reconciliation_outcome = 'UNPROCESSABLE'
        OR (account_reference_version IS NOT NULL
            AND account_reference_hash IS NOT NULL)),

    CONSTRAINT uq_recon_run      UNIQUE (run_id),
    CONSTRAINT uq_recon_identity UNIQUE (result_id, run_id)
);

-- ---------------------------------------------------------------------------
-- exception_items.
--
-- exception_result_binding is a COMPOSITE foreign key, so an exception naming
-- one run while its result names another is structurally impossible.
--
-- expected_value and actual_value are TEXT holding the exact decimal string,
-- so a reported variance is never distorted by a float round trip on its way
-- to a screen. No account code is ever cast to a numeric type.
-- ---------------------------------------------------------------------------
CREATE TABLE exception_items (
    exception_id    UUID        PRIMARY KEY,
    result_id       UUID        NOT NULL,
    run_id          UUID        NOT NULL,
    correlation_id  UUID        NOT NULL,
    exception_code  TEXT        NOT NULL,
    severity        TEXT        NOT NULL,
    line_reference  TEXT        NULL,
    expected_value  TEXT        NULL,
    actual_value    TEXT        NULL,
    detail          JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT exception_code_valid CHECK (exception_code IN (
        'NO_PO_REFERENCE', 'PO_NOT_FOUND', 'PO_NOT_ALLOW_LISTED',
        'PO_STATUS_NOT_ELIGIBLE', 'SUPPLIER_MISMATCH', 'CURRENCY_MISMATCH',
        'LINE_COUNT_MISMATCH', 'UNPAIRED_LINE', 'AMBIGUOUS_MULTIPLE_CANDIDATES',
        'QUANTITY_VARIANCE', 'UNIT_PRICE_VARIANCE', 'LINE_AMOUNT_VARIANCE',
        'TAX_TYPE_MISMATCH', 'MISSING_TAX_TYPE', 'LINE_TAX_VARIANCE',
        'TAX_VARIANCE', 'TOTAL_VARIANCE', 'MISSING_BILL_ACCOUNT_CODE',
        'MISSING_PO_ACCOUNT_CODE', 'INVALID_BILL_ACCOUNT_CODE', 'INVALID_PO_ACCOUNT_CODE',
        'UNKNOWN_BILL_ACCOUNT_CODE', 'UNKNOWN_PO_ACCOUNT_CODE', 'ACCOUNT_CODE_MISMATCH',
        'DUPLICATE_INVOICE_NUMBER', 'DUPLICATE_BUSINESS_KEY', 'RESIDUAL_QUANTITY_VARIANCE',
        'RESIDUAL_UNIT_PRICE_VARIANCE', 'RESIDUAL_LINE_AMOUNT_VARIANCE', 'RESIDUAL_TAX_TYPE_MISMATCH',
        'RESIDUAL_MISSING_TAX_TYPE', 'RESIDUAL_LINE_TAX_VARIANCE', 'RESIDUAL_MISSING_BILL_ACCOUNT_CODE',
        'RESIDUAL_MISSING_PO_ACCOUNT_CODE', 'RESIDUAL_INVALID_BILL_ACCOUNT_CODE', 'RESIDUAL_INVALID_PO_ACCOUNT_CODE',
        'RESIDUAL_UNKNOWN_BILL_ACCOUNT_CODE', 'RESIDUAL_UNKNOWN_PO_ACCOUNT_CODE', 'RESIDUAL_ACCOUNT_CODE_MISMATCH')),

    CONSTRAINT exception_severity_valid
        CHECK (severity IN ('BLOCKING', 'ADVISORY')),
    CONSTRAINT exception_detail_bounded
        CHECK (octet_length(detail::text) <= 4096),
    CONSTRAINT exception_result_binding
        FOREIGN KEY (result_id, run_id)
        REFERENCES reconciliation_results (result_id, run_id)
);

CREATE UNIQUE INDEX uq_exception_per_result
    ON exception_items (result_id, exception_code, COALESCE(line_reference, ''));
CREATE INDEX idx_exceptions_run  ON exception_items (run_id);
CREATE INDEX idx_exceptions_code ON exception_items (exception_code, created_at);

-- ---------------------------------------------------------------------------
-- The semantic stage column. Reconciliation owns atomic initialisation, so it
-- is added here rather than in 004.
--
-- REVIEW_READY resolves a real contradiction: a design cannot hold that the
-- reconciler writes the outcome, that a review case receives REVIEW_REQUIRED,
-- and that RECONCILING requires a null outcome, while leaving review cases in
-- RECONCILING during semantic review. REVIEW_READY is where a review case sits
-- from the moment its outcome is persisted until notification is queued.
-- semantic_stage_status, not another workflow status, records that progress.
-- ---------------------------------------------------------------------------
ALTER TABLE runs ADD COLUMN semantic_stage_status TEXT NOT NULL DEFAULT 'NOT_REQUIRED';

ALTER TABLE runs
  ADD CONSTRAINT runs_semantic_stage_valid CHECK (semantic_stage_status IN (
        'NOT_REQUIRED', 'DISABLED', 'PENDING', 'IN_PROGRESS', 'RETRY_PENDING',
        'COMPLETED_WITH_RECOMMENDATION', 'COMPLETED_WITHOUT_RECOMMENDATION')),

  ADD CONSTRAINT runs_semantic_stage_requires_review CHECK (
        semantic_stage_status = 'NOT_REQUIRED'
        OR reconciliation_outcome IS NOT DISTINCT FROM 'REVIEW_REQUIRED'),

  ADD CONSTRAINT runs_nonterminal_semantic_requires_review_ready CHECK (
        semantic_stage_status NOT IN ('PENDING', 'IN_PROGRESS', 'RETRY_PENDING')
        OR (workflow_status = 'REVIEW_READY'
            AND reconciliation_outcome IS NOT DISTINCT FROM 'REVIEW_REQUIRED')),

  ADD CONSTRAINT runs_nonreview_semantic_not_required CHECK (
        reconciliation_outcome IS NULL
        OR reconciliation_outcome NOT IN ('MATCHED', 'UNPROCESSABLE')
        OR semantic_stage_status = 'NOT_REQUIRED'),

  ADD CONSTRAINT runs_human_states_require_terminal_semantics CHECK (
        (workflow_status NOT IN ('NOTIFY_PENDING', 'AWAITING_TRIAGE', 'TRIAGED',
                                 'ACTION_PENDING', 'ACTION_FAILED')
         AND NOT (workflow_status = 'COMPLETED'
                  AND reconciliation_outcome = 'REVIEW_REQUIRED'))
        OR semantic_stage_status IN (
            'NOT_REQUIRED', 'DISABLED', 'COMPLETED_WITH_RECOMMENDATION',
            'COMPLETED_WITHOUT_RECOMMENDATION'));

GRANT SELECT, INSERT, UPDATE ON reconciliation_results TO bpr_app;
GRANT SELECT, INSERT         ON exception_items        TO bpr_app;
REVOKE DELETE ON reconciliation_results, exception_items FROM bpr_app;
REVOKE UPDATE ON exception_items FROM bpr_app;

COMMIT;
