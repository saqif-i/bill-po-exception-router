"""Notification and the human decision.

Volume 09 sections 9.5, 9.7 and 9.9.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from psycopg import Connection

from policy_service.domain.enums import TriageAction, TriageDestination
from policy_service.integrations.slack_blocks import (
    CHANNEL_FOR,
    build_card,
    build_decided_card,
)


class NotifyRefusedError(RuntimeError):
    """The run is not in a state where a card may be posted."""


class DecisionRefusedError(RuntimeError):
    """The interaction cannot become a decision."""


@dataclass
class CardContext:
    run_id: uuid.UUID
    invoice_number: str
    destination: TriageDestination
    exception_codes: list[str]
    recommendation: dict | None
    semantic_gate_reason: str
    human_review_reasons: list[str]


def load_card_context(
    conn: Connection, run_id: uuid.UUID, *, allowed_statuses: tuple[str, ...]
) -> CardContext:
    """Everything the card shows, with a state check the caller chooses.

    Notification demands REVIEW_READY (invariant I31). Recording a decision must
    also accept AWAITING_TRIAGE, because notification itself moved the run
    there: a reviewer clicking a button they were just shown is the normal path,
    not a violation.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.workflow_status, r.reconciliation_outcome, r.semantic_stage_status,
                   r.triage_destination, r.xero_invoice_number, r.human_review_reasons,
                   rr.semantic_gate_reason
              FROM runs r JOIN reconciliation_results rr USING (run_id)
             WHERE r.run_id = %s
            """,
            (run_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise NotifyRefusedError("RUN_NOT_FOUND")
        (status, outcome, stage, destination, number, reasons, gate_reason) = row

        cur.execute(
            "SELECT 1 FROM semantic_attempts WHERE run_id = %s AND status = 'STARTED'",
            (run_id,),
        )
        if cur.fetchone() is not None:
            raise NotifyRefusedError("SEMANTIC_ATTEMPT_STILL_STARTED")

        if outcome != "REVIEW_REQUIRED":
            raise NotifyRefusedError("OUTCOME_NOT_REVIEW_REQUIRED")
        if status not in allowed_statuses:
            raise NotifyRefusedError(f"WORKFLOW_STATUS_{status}")
        if stage not in (
            "NOT_REQUIRED",
            "DISABLED",
            "COMPLETED_WITH_RECOMMENDATION",
            "COMPLETED_WITHOUT_RECOMMENDATION",
        ):
            raise NotifyRefusedError(f"SEMANTIC_STAGE_NOT_TERMINAL_{stage}")

        cur.execute(
            "SELECT exception_code FROM exception_items WHERE run_id = %s "
            "ORDER BY created_at, exception_code",
            (run_id,),
        )
        codes = [r[0] for r in cur.fetchall()]

        recommendation = None
        if stage == "COMPLETED_WITH_RECOMMENDATION":
            cur.execute(
                "SELECT recommendation, confidence, explanation, evidence "
                "FROM semantic_attempts WHERE run_id = %s AND status = 'SUCCEEDED' "
                "ORDER BY attempt_number DESC LIMIT 1",
                (run_id,),
            )
            found = cur.fetchone()
            if found is not None:
                recommendation = {
                    "recommendation": found[0],
                    "confidence": float(found[1]),
                    "explanation": found[2],
                    "evidence": found[3],
                }

    return CardContext(
        run_id=run_id,
        invoice_number=number,
        destination=TriageDestination(destination),
        exception_codes=codes,
        recommendation=recommendation,
        semantic_gate_reason=gate_reason,
        human_review_reasons=list(reasons),
    )


def load_for_notification(conn: Connection, run_id: uuid.UUID) -> CardContext:
    """I31: a run observed in REVIEW_READY, with a terminal semantic stage and
    no STARTED attempt.

    The terminal-stage requirement is the ordering gate. A card posted while the
    model stage is still running would change under the reviewer.
    """
    return load_card_context(conn, run_id, allowed_statuses=("REVIEW_READY",))


def notify(conn: Connection, *, run_id: uuid.UUID, correlation_id: uuid.UUID, client) -> dict:
    """Post the card, then record what happened. Idempotent per run.

    The idempotency check comes FIRST. Checking the workflow status before it
    would refuse a harmless second call, because the first call already moved
    the run to AWAITING_TRIAGE.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT message_ts, post_status FROM slack_notifications WHERE run_id = %s",
            (run_id,),
        )
        existing = cur.fetchone()
    if existing is not None and existing[1] == "POSTED":
        return {"posted": False, "already": True, "message_ts": existing[0]}

    context = load_for_notification(conn, run_id)
    channel = CHANNEL_FOR[context.destination]

    blocks = build_card(
        run_id=str(run_id),
        invoice_number=context.invoice_number,
        destination=context.destination,
        exception_codes=context.exception_codes,
        recommendation=context.recommendation,
        semantic_gate_reason=context.semantic_gate_reason,
        human_review_reasons=context.human_review_reasons,
    )
    outcome = client.post_card(
        channel=channel,
        blocks=blocks,
        text=f"Bill {context.invoice_number} needs review",
    )

    status = (
        "POSTED" if outcome.ok else "POSSIBLE_DUPLICATE" if outcome.dispatch_unknown else "FAILED"
    )
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO slack_notifications (notification_id, run_id, correlation_id,
                channel, destination, message_ts, post_status, post_error)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (run_id) DO UPDATE
               SET message_ts = EXCLUDED.message_ts,
                   post_status = EXCLUDED.post_status,
                   post_error = EXCLUDED.post_error
            """,
            (
                uuid.uuid4(),
                run_id,
                correlation_id,
                # The ID Slack resolved the name to, when the post succeeded.
                # `chat.postMessage` accepts a channel NAME, but `chat.update`
                # requires the ID: posting by name and then updating by name
                # fails with `channel_not_found`, and the decision is recorded
                # while the card silently stays stale.
                #
                # Falls back to the name so a failed post still records where it
                # was aimed.
                outcome.channel_id or channel,
                context.destination.value,
                outcome.message_ts,
                status,
                outcome.error or None,
            ),
        )
        if outcome.ok:
            cur.execute(
                "UPDATE runs SET workflow_status = 'AWAITING_TRIAGE', updated_at = now() "
                "WHERE run_id = %s AND workflow_status = 'REVIEW_READY'",
                (run_id,),
            )

    return {
        "posted": outcome.ok,
        "status": status,
        "channel": channel,
        "message_ts": outcome.message_ts,
        "error": outcome.error or None,
    }


def record_decision(
    conn: Connection,
    *,
    run_id: uuid.UUID,
    action: TriageAction,
    user_id: str,
    user_name: str | None,
    interaction_id: str,
    message_ts: str | None,
) -> dict:
    """The decision, in ONE transaction, committed BEFORE Slack is acknowledged.

    Invariant I11, at its reduced surface: the XERO_HISTORY_NOTE half of the
    original transaction has no subject in this build, but the ordering that
    matters is unchanged. Acknowledging first and writing afterwards would mean
    a crash between them loses a decision a person believes they made.
    """
    # The retry check comes FIRST, before any state check. The first delivery
    # moved the run to COMPLETED, so a retry would otherwise be refused as a
    # state violation rather than recognised as the duplicate it is.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT decision_id FROM triage_decisions WHERE slack_interaction_id = %s",
            (interaction_id,),
        )
        if cur.fetchone() is not None:
            return {"recorded": False, "reason": "ALREADY_RECORDED", "context": None}

    context = load_card_context(conn, run_id, allowed_statuses=("AWAITING_TRIAGE", "REVIEW_READY"))

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO triage_decisions (decision_id, run_id, correlation_id,
                action, decided_by, decided_by_name, shown_exception_codes,
                shown_destination, shown_recommendation, shown_confidence,
                shown_gate_reason, slack_message_ts, slack_interaction_id)
            VALUES (%s, %s, (SELECT correlation_id FROM runs WHERE run_id = %s),
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (slack_interaction_id) DO NOTHING
            RETURNING decision_id
            """,
            (
                uuid.uuid4(),
                run_id,
                run_id,
                action.value,
                user_id,
                user_name,
                context.exception_codes,
                context.destination.value,
                (context.recommendation or {}).get("recommendation"),
                (context.recommendation or {}).get("confidence"),
                context.semantic_gate_reason,
                message_ts,
                interaction_id,
            ),
        )
        row = cur.fetchone()
        if row is None:
            # Slack retried a delivery it believed failed. Not an error.
            return {"recorded": False, "reason": "ALREADY_RECORDED", "context": context}

        cur.execute(
            "UPDATE runs SET workflow_status = 'COMPLETED', updated_at = now() "
            "WHERE run_id = %s AND workflow_status IN ('AWAITING_TRIAGE', 'REVIEW_READY')",
            (run_id,),
        )
        cur.execute(
            """
            INSERT INTO integration_events (event_id, correlation_id, run_id,
                event_type, event_status, source, occurred_at)
            VALUES (%s, (SELECT correlation_id FROM runs WHERE run_id = %s), %s,
                    'TRIAGE_DECISION_RECORDED', 'SUCCEEDED', 'SLACK_INBOUND', now())
            """,
            (uuid.uuid4(), run_id, run_id),
        )

    return {"recorded": True, "decision_id": str(row[0]), "context": context}


def update_card_best_effort(
    conn: Connection,
    *,
    run_id: uuid.UUID,
    context: CardContext,
    action: TriageAction,
    user_id: str,
    decided_at: str,
    client,
) -> bool:
    """After the decision commits. The database is authoritative; this is a view.

    A lost response leaves the card stale while the decision is correctly
    recorded, which is ADR-007 and is not claimed to be otherwise.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT channel, message_ts FROM slack_notifications WHERE run_id = %s",
            (run_id,),
        )
        row = cur.fetchone()
    if row is None or not row[1]:
        return False  # never posted, or posted with an unknown outcome

    outcome = client.update_card(
        channel=row[0],
        message_ts=row[1],
        blocks=build_decided_card(
            invoice_number=context.invoice_number,
            action=action,
            decided_by=user_id,
            decided_at=decided_at,
            destination=context.destination,
        ),
        text=f"Bill {context.invoice_number} triaged",
    )
    if outcome.ok:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE slack_notifications SET card_updated_at = now() WHERE run_id = %s",
                (run_id,),
            )
    return outcome.ok
