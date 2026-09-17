"""The semantic stage: the gate, the lifecycle, and the persistence.

Volume 08 sections 9.3, 9.4, 9.6 and 9.14.

The gate itself already ran: the reconciler recorded `semantic_permitted` and a
`semantic_gate_reason` on every run. This module re-checks the persisted state
before calling anything, because between reconciliation and here the run may
have moved.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from psycopg import Connection

from policy_service.domain.enums import HumanReviewReason, SemanticStageStatus
from policy_service.integrations.claude_contract import (
    Rejection,
    RejectionReason,
    ValidatedRecommendation,
    validate,
)

SCHEMA_VERSION = "semantic_review.strict.v1"

# A rejection reason maps to the human-review reason shown on the card.
_REVIEW_REASON = {
    RejectionReason.MALFORMED: HumanReviewReason.SEMANTIC_OUTPUT_INVALID,
    RejectionReason.EVIDENCE_UNSUPPORTED: HumanReviewReason.SEMANTIC_EVIDENCE_UNSUPPORTED,
    RejectionReason.LOW_CONFIDENCE: HumanReviewReason.SEMANTIC_LOW_CONFIDENCE,
    RejectionReason.REFUSED: HumanReviewReason.SEMANTIC_REFUSED,
    RejectionReason.TRUNCATED: HumanReviewReason.SEMANTIC_TRUNCATED,
    RejectionReason.UNEXPECTED_STOP: HumanReviewReason.SEMANTIC_UNEXPECTED_STOP_REASON,
}


class GateRefusedError(RuntimeError):
    """The persisted state does not permit an invocation."""


@dataclass
class ResidualPair:
    purchase_order_line_description: str
    bill_line_description: str


def load_eligible_run(conn: Connection, run_id: uuid.UUID) -> ResidualPair:
    """Re-check the persisted state, then extract the two descriptions.

    I32: the run must still be REVIEW_READY with the stage PENDING. Anything
    else means the run moved on, and a recommendation arriving now would attach
    to a decision that has already been made.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.workflow_status, r.reconciliation_outcome,
                   r.semantic_stage_status, rr.semantic_permitted,
                   rr.residual_comparison
              FROM runs r
              JOIN reconciliation_results rr USING (run_id)
             WHERE r.run_id = %s
            """,
            (run_id,),
        )
        row = cur.fetchone()

    if row is None:
        raise GateRefusedError("RUN_NOT_FOUND")
    status, outcome, stage, permitted, residual = row

    if outcome != "REVIEW_REQUIRED":
        raise GateRefusedError("OUTCOME_NOT_REVIEW_REQUIRED")
    if status != "REVIEW_READY":
        raise GateRefusedError(f"WORKFLOW_STATUS_{status}")
    if stage != SemanticStageStatus.PENDING.value:
        raise GateRefusedError(f"SEMANTIC_STAGE_{stage}")
    if not permitted:
        raise GateRefusedError("GATE_CLOSED")
    if not residual or not residual.get("all_checks_passed"):
        # Defence in depth. The reconciler already refuses this case; if the two
        # ever disagree, the safe answer is to refuse.
        raise GateRefusedError("RESIDUAL_NOT_CLEAN")

    return ResidualPair(
        purchase_order_line_description=residual["po_line_description"],
        bill_line_description=residual["bill_line_description"],
    )


def start_attempt(
    conn: Connection,
    *,
    run_id: uuid.UUID,
    correlation_id: uuid.UUID,
    model_id: str,
    enabled_flag: bool,
) -> uuid.UUID:
    """Committed BEFORE the provider call.

    A crash between this and the response leaves a STARTED row, which is
    visible, rather than a call nobody recorded. The partial unique index means
    a second concurrent attempt for the same run is refused by the database.
    """
    attempt_id = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT coalesce(max(attempt_number), 0) + 1 FROM semantic_attempts WHERE run_id = %s",
            (run_id,),
        )
        number = cur.fetchone()[0]
        cur.execute(
            """
            INSERT INTO semantic_attempts (
                attempt_id, run_id, correlation_id, attempt_number, status,
                model_id, prompt_version, schema_version,
                semantic_review_enabled_at_attempt
            ) VALUES (%s, %s, %s, %s, 'STARTED', %s, %s, %s, %s)
            """,
            (
                attempt_id,
                run_id,
                correlation_id,
                number,
                model_id,
                "line_semantics.v1",
                SCHEMA_VERSION,
                enabled_flag,
            ),
        )
        cur.execute(
            "UPDATE runs SET semantic_stage_status = 'IN_PROGRESS', "
            "updated_at = now() WHERE run_id = %s AND semantic_stage_status = 'PENDING'",
            (run_id,),
        )
        if cur.rowcount != 1:
            raise GateRefusedError("STAGE_MOVED")
    return attempt_id


def finalise(
    conn: Connection,
    *,
    run_id: uuid.UUID,
    attempt_id: uuid.UUID,
    result: ValidatedRecommendation | Rejection,
) -> dict:
    """Exactly one transition, and the run moves with it.

    I06: on ANY rejection the run reaches COMPLETED_WITHOUT_RECOMMENDATION and
    the human sees the case with no model output at all, rather than a hedged or
    partial one.
    """
    with conn.cursor() as cur:
        if isinstance(result, ValidatedRecommendation):
            from psycopg.types.json import Jsonb

            cur.execute(
                """
                UPDATE semantic_attempts
                   SET status='SUCCEEDED', recommendation=%s, confidence=%s,
                       explanation=%s, evidence=%s, finalised_at=now()
                 WHERE attempt_id=%s AND status='STARTED'
                """,
                (
                    result.recommendation.value,
                    result.confidence,
                    result.explanation,
                    Jsonb(list(result.evidence)),
                    attempt_id,
                ),
            )
            moved = cur.rowcount
            stage = "COMPLETED_WITH_RECOMMENDATION"
            review_reason = HumanReviewReason.SEMANTIC_RECOMMENDATION_AVAILABLE
        else:
            cur.execute(
                """
                UPDATE semantic_attempts
                   SET status='REJECTED', rejection_reason=%s, rejection_detail=%s,
                       finalised_at=now()
                 WHERE attempt_id=%s AND status='STARTED'
                """,
                (result.reason.value, result.detail[:500], attempt_id),
            )
            moved = cur.rowcount
            stage = "COMPLETED_WITHOUT_RECOMMENDATION"
            review_reason = _REVIEW_REASON[result.reason]

        # I32: a result arriving after abandonment affects zero rows.
        if moved != 1:
            return {"applied": False, "reason": "ATTEMPT_NO_LONGER_STARTED"}

        cur.execute(
            """
            UPDATE runs
               SET semantic_stage_status = %s,
                   human_review_reasons = array_append(human_review_reasons, %s),
                   updated_at = now()
             WHERE run_id = %s AND semantic_stage_status = 'IN_PROGRESS'
            """,
            (stage, review_reason.value, run_id),
        )

        cur.execute(
            """
            INSERT INTO integration_events (event_id, correlation_id, run_id,
                event_type, event_status, source, occurred_at)
            VALUES (%s, (SELECT correlation_id FROM runs WHERE run_id=%s), %s,
                    'SEMANTIC_ATTEMPT_FINALISED', %s, 'SEMANTIC', now())
            """,
            (
                uuid.uuid4(),
                run_id,
                run_id,
                "SUCCEEDED" if isinstance(result, ValidatedRecommendation) else "FAILED",
            ),
        )

    return {"applied": True, "semantic_stage_status": stage}


def review(
    conn: Connection,
    *,
    run_id: uuid.UUID,
    correlation_id: uuid.UUID,
    client,
    min_confidence: float,
    enabled_flag: bool,
) -> dict:
    """The whole lifecycle. The only function the endpoint calls."""
    pair = load_eligible_run(conn, run_id)
    attempt_id = start_attempt(
        conn,
        run_id=run_id,
        correlation_id=correlation_id,
        model_id=client.model,
        enabled_flag=enabled_flag,
    )
    conn.commit()  # the attempt is durable before the call leaves the process

    provider = client.review(
        purchase_order_line_description=pair.purchase_order_line_description,
        bill_line_description=pair.bill_line_description,
    )

    if provider.rejection is not None:
        outcome = provider.rejection
    else:
        outcome = validate(
            provider.raw,
            purchase_order_line_description=pair.purchase_order_line_description,
            bill_line_description=pair.bill_line_description,
            min_confidence=min_confidence,
        )

    summary = finalise(conn, run_id=run_id, attempt_id=attempt_id, result=outcome)
    conn.commit()

    summary["recommendation"] = (
        outcome.recommendation.value if isinstance(outcome, ValidatedRecommendation) else None
    )
    return summary
