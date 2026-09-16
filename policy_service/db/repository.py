"""Persistence for a reconciliation decision.

Volume 06 section 9.13. The reconciliation transaction is atomic across its
COMPLETE decision record: the account-reference snapshot with its hash and
version, reconciliation_results, every exception_items row,
runs.reconciliation_outcome, the correct next workflow_status, the initialised
semantic stage and the event.

There is no window in which an outcome exists without its exceptions, or in
which a review case sits in RECONCILING with an outcome already set. That is
invariant I28, and the database enforces it too: runs_pre_reconciliation_has_
no_outcome would reject the intermediate state if this code tried to write it
in two steps.
"""

from __future__ import annotations

import uuid
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb

from policy_service.domain.enums import (
    SEMANTIC_BLOCKING_CODES,
    ExceptionCode,
    Outcome,
)
from policy_service.domain.reconciliation import ReconciliationResult
from policy_service.integrations.xero_parsing import dumps

# The workflow status a run moves to, by outcome (section 9.13).
_NEXT_STATUS = {
    Outcome.MATCHED: "COMPLETED",
    Outcome.UNPROCESSABLE: "COMPLETED",
    Outcome.REVIEW_REQUIRED: "REVIEW_READY",
}


def _severity(code: ExceptionCode) -> str:
    """BLOCKING means this alone prevents a model call."""
    return "BLOCKING" if code in SEMANTIC_BLOCKING_CODES else "ADVISORY"


def _jsonb(value: Any) -> Jsonb:
    """Round-trip through the Decimal-safe encoder so no Decimal reaches the
    driver as a float."""
    import json

    return Jsonb(json.loads(dumps(value)))


def persist_reconciliation(
    conn: Connection,
    *,
    run_id: uuid.UUID,
    correlation_id: uuid.UUID,
    result: ReconciliationResult,
    tolerance_version: str,
    account_reference_version: str | None,
    account_reference_hash: str | None,
    po_number: str | None = None,
    xero_po_id: uuid.UUID | None = None,
    line_comparisons: list[dict] | None = None,
) -> uuid.UUID:
    """Write the whole decision in ONE transaction. Returns the result id.

    The caller owns the transaction boundary. Nothing here commits, and no
    external call may occur between the first write and the commit (I12).
    """
    result_id = uuid.uuid4()

    with conn.cursor() as cur:
        # 1. the result
        cur.execute(
            """
            INSERT INTO reconciliation_results (
                result_id, run_id, correlation_id, reconciliation_outcome,
                tolerance_version, account_reference_version, account_reference_hash,
                po_number, xero_po_id, paired_line_count,
                unpaired_bill_lines, unpaired_po_lines,
                semantic_permitted, semantic_gate_reason,
                line_comparisons, residual_comparison
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                result_id,
                run_id,
                correlation_id,
                result.outcome.value,
                tolerance_version,
                account_reference_version,
                account_reference_hash,
                po_number,
                xero_po_id,
                result.paired_line_count,
                len(result.unpaired_bill_lines),
                len(result.unpaired_po_lines),
                result.semantic_permitted,
                result.semantic_gate_reason.value,
                _jsonb(line_comparisons or []),
                _jsonb(result.residual_comparison) if result.residual_comparison else None,
            ),
        )

        # 2. every exception
        for item in result.exceptions:
            cur.execute(
                """
                INSERT INTO exception_items (
                    exception_id, result_id, run_id, correlation_id,
                    exception_code, severity, line_reference, detail
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (result_id, exception_code, COALESCE(line_reference, ''))
                DO NOTHING
                """,
                (
                    uuid.uuid4(),
                    result_id,
                    run_id,
                    correlation_id,
                    item.code.value,
                    _severity(item.code),
                    item.line_reference,
                    _jsonb(item.detail),
                ),
            )

        # 3. the run: outcome, next status and the semantic stage together.
        #
        # One statement, not three. Writing the outcome first would create a
        # run in RECONCILING with an outcome set, which the database rejects
        # and which I28 forbids.
        cur.execute(
            """
            UPDATE runs
               SET reconciliation_outcome = %s,
                   workflow_status        = %s,
                   unprocessable_reason   = %s,
                   semantic_stage_status  = %s,
                   triage_destination     = %s,
                   human_review_reasons   = %s,
                   duplicate_invoice_key  = %s,
                   duplicate_business_key = %s,
                   updated_at             = now()
             WHERE run_id = %s
            """,
            (
                result.outcome.value,
                _NEXT_STATUS[result.outcome],
                result.unprocessable_reason.value if result.unprocessable_reason else None,
                result.semantic_stage_status.value,
                result.triage_destination.value if result.triage_destination else None,
                [r.value for r in result.human_review_reasons],
                result.duplicate_invoice_key,
                result.duplicate_business_key,
                run_id,
            ),
        )

        # 4. the event, in the same transaction, recording the captured flag so
        # a later configuration change cannot reinterpret this run.
        cur.execute(
            """
            INSERT INTO integration_events (
                event_id, correlation_id, run_id, event_type, event_status,
                source, occurred_at
            ) VALUES (%s, %s, %s, 'SEMANTIC_STAGE_INITIALISED', 'SUCCEEDED',
                      'RECONCILER', now())
            """,
            (uuid.uuid4(), correlation_id, run_id),
        )

    return result_id


def find_duplicate_hits(
    conn: Connection,
    *,
    run_id: uuid.UUID,
    invoice_key: str | None,
    business_key: str | None,
) -> tuple[bool, bool]:
    """Duplicate LOOKUPS live here so the engine stays pure.

    A collision with a previously ingested run that is not this run raises the
    exception. Comparing against itself would make every bill a duplicate of
    itself.
    """
    invoice_hit = business_hit = False
    with conn.cursor() as cur:
        if invoice_key:
            cur.execute(
                "SELECT 1 FROM runs WHERE duplicate_invoice_key = %s AND run_id <> %s LIMIT 1",
                (invoice_key, run_id),
            )
            invoice_hit = cur.fetchone() is not None
        if business_key:
            cur.execute(
                "SELECT 1 FROM runs WHERE duplicate_business_key = %s AND run_id <> %s LIMIT 1",
                (business_key, run_id),
            )
            business_hit = cur.fetchone() is not None
    return invoice_hit, business_hit
