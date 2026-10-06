"""Notification and the human decision.

Invariants I03, I12 and I31.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from psycopg import Connection

from policy_service.domain.enums import TriageAction, TriageDestination
from policy_service.integrations.slack_blocks import (
    ACTIONS_FOR,
    CHANNEL_FOR,
    build_card,
    build_decided_card,
    escape_mrkdwn,
)
from policy_service.integrations.slack_client import PostOutcome


class NotifyRefusedError(RuntimeError):
    """The run is not in a state where a card may be posted."""


class DecisionRefusedError(RuntimeError):
    """The interaction cannot become a decision."""


# The actions that hand a case to another team instead of closing it. The
# team's new card carries its own controls, and its decision closes the run.
HANDOFFS: dict[TriageAction, TriageDestination] = {
    TriageAction.SEND_TO_FINANCE: TriageDestination.FINANCE,
    TriageAction.SEND_TO_PROCUREMENT: TriageDestination.PROCUREMENT,
}


@dataclass
class CardContext:
    run_id: uuid.UUID
    invoice_number: str
    destination: TriageDestination
    exception_codes: list[str]
    recommendation: dict | None
    semantic_gate_reason: str
    human_review_reasons: list[str]
    # Who handed the case to this destination, if anyone: the latest hand-off.
    handed_over: dict | None = None


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
               FOR UPDATE OF r
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

        cur.execute(
            "SELECT decided_by, shown_destination FROM triage_decisions "
            "WHERE run_id = %s AND NOT is_final ORDER BY decided_at DESC LIMIT 1",
            (run_id,),
        )
        handoff = cur.fetchone()

    return CardContext(
        run_id=run_id,
        invoice_number=number,
        destination=TriageDestination(destination),
        exception_codes=codes,
        recommendation=recommendation,
        semantic_gate_reason=gate_reason,
        human_review_reasons=list(reasons),
        handed_over=(
            {"by": handoff[0], "from": TriageDestination(handoff[1])} if handoff else None
        ),
    )


def awaits_card(conn: Connection, run_id: uuid.UUID) -> bool:
    """True when the run is ready for a card that its current destination has
    not been sent: after a hand-off, the card on record is the previous team's."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT r.workflow_status, r.triage_destination, n.post_status, n.destination "
            "FROM runs r LEFT JOIN slack_notifications n USING (run_id) WHERE r.run_id = %s",
            (run_id,),
        )
        row = cur.fetchone()
    if row is None or row[0] != "REVIEW_READY":
        return False
    return not (row[2] == "POSTED" and row[3] == row[1])


def load_for_notification(conn: Connection, run_id: uuid.UUID) -> CardContext:
    """I31: a run observed in REVIEW_READY, with a terminal semantic stage and
    no STARTED attempt.

    The terminal-stage requirement is the ordering gate. A card posted while the
    model stage is still running would change under the reviewer.
    """
    return load_card_context(conn, run_id, allowed_statuses=("REVIEW_READY",))


def notify(conn: Connection, *, run_id: uuid.UUID, correlation_id: uuid.UUID, client) -> dict:
    """Post the card, then record what happened. Idempotent per run.

    Three steps, so no transaction or lock is held while Slack is called (I12):

    1. Lock the run, check it (I31), and move it to NOTIFY_PENDING. Committed,
       which releases the lock. A second notify now sees NOTIFY_PENDING and is
       refused, so the claim does what the lock did without being held.
    2. Call Slack, with no transaction open.
    3. Record the result. A posted card moves the run to AWAITING_TRIAGE.
       Anything else returns it to REVIEW_READY so a retry can post.

    A process that dies during step 2 leaves the run in NOTIFY_PENDING, which
    the stuck-run check reports. See docs/runbook.md section 1.

    The idempotency check comes FIRST. Checking the workflow status before it
    would refuse a harmless second call, because the first call already moved
    the run to AWAITING_TRIAGE.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT n.message_ts, n.post_status, n.destination, r.triage_destination "
            "FROM runs r LEFT JOIN slack_notifications n USING (run_id) WHERE r.run_id = %s",
            (run_id,),
        )
        existing = cur.fetchone()
    # Posted for the run's current destination. After a hand-off the card on
    # record is the previous team's, so the new team's card is still owed.
    if existing is not None and existing[1] == "POSTED" and existing[2] == existing[3]:
        return {"posted": False, "already": True, "message_ts": existing[0]}

    context = load_for_notification(conn, run_id)
    channel = CHANNEL_FOR[context.destination]
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE runs SET workflow_status = 'NOTIFY_PENDING', updated_at = now() "
            "WHERE run_id = %s AND workflow_status = 'REVIEW_READY'",
            (run_id,),
        )
    conn.commit()  # releases the row lock before the call leaves the process

    blocks = build_card(
        run_id=str(run_id),
        invoice_number=context.invoice_number,
        destination=context.destination,
        exception_codes=context.exception_codes,
        recommendation=context.recommendation,
        semantic_gate_reason=context.semantic_gate_reason,
        human_review_reasons=context.human_review_reasons,
        handed_over=context.handed_over,
    )
    try:
        outcome = client.post_card(
            channel=channel,
            blocks=blocks,
            text=f"Bill {escape_mrkdwn(context.invoice_number)} needs review",
        )
    except Exception as exc:
        # The request may have gone out, so this is unknown rather than failed.
        # Raising here would leave the run in NOTIFY_PENDING with no record.
        outcome = PostOutcome(False, error=type(exc).__name__, dispatch_unknown=True)

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
               SET channel = EXCLUDED.channel,
                   destination = EXCLUDED.destination,
                   message_ts = EXCLUDED.message_ts,
                   post_status = EXCLUDED.post_status,
                   post_error = EXCLUDED.post_error,
                   card_updated_at = NULL
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
                #
                # On a second card for the run (after a hand-off) every field is
                # replaced, channel included. Keeping the first card's channel
                # paired the new card's timestamp with the old channel, so the
                # update after the decision edited nothing and left the new
                # card's buttons live.
                outcome.channel_id or channel,
                context.destination.value,
                outcome.message_ts,
                status,
                outcome.error or None,
            ),
        )
        cur.execute(
            "UPDATE runs SET workflow_status = %s, updated_at = now() "
            "WHERE run_id = %s AND workflow_status = 'NOTIFY_PENDING'",
            ("AWAITING_TRIAGE" if outcome.ok else "REVIEW_READY", run_id),
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

    A hand-off (HANDOFFS) is recorded the same way but does not close the run:
    the run moves to the new destination and back to REVIEW_READY, so notify
    posts that team's card, and that team's decision closes it. The result
    carries `handoff_to` so the caller can post the card at once.

    Only the current card's controls count. A click on a card a hand-off has
    replaced, or on a control the current destination does not offer, is
    refused: that card is no longer where the case is.
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

    # NOTIFY_PENDING too: a click can only come from a card Slack has posted,
    # and it can arrive before notify has recorded that post.
    context = load_card_context(
        conn, run_id, allowed_statuses=("AWAITING_TRIAGE", "NOTIFY_PENDING", "REVIEW_READY")
    )
    if action not in ACTIONS_FOR[context.destination]:
        raise DecisionRefusedError(f"ACTION_NOT_OFFERED_FOR_{context.destination.value}")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT message_ts, post_status, destination FROM slack_notifications "
            "WHERE run_id = %s",
            (run_id,),
        )
        card = cur.fetchone()
    # The clicked card must be the current one: posted, for the run's current
    # team, and the card that was clicked. Comparing timestamps alone accepted
    # the old AP card after a hand-off, both before the new team's card was
    # recorded and after it failed to post, so a case "sent to finance" could
    # be closed without finance seeing it. No record at all means the run's
    # first card is being posted, and a click can only have come from it.
    current = card is not None and (
        card[1] == "POSTED"
        and card[2] == context.destination.value
        and bool(message_ts)
        and card[0] == message_ts
    )
    if card is not None and not current:
        raise DecisionRefusedError("CARD_SUPERSEDED")

    handoff_to = HANDOFFS.get(action)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO triage_decisions (decision_id, run_id, correlation_id,
                action, decided_by, decided_by_name, shown_exception_codes,
                shown_destination, shown_recommendation, shown_confidence,
                shown_gate_reason, slack_message_ts, slack_interaction_id, is_final)
            VALUES (%s, %s, (SELECT correlation_id FROM runs WHERE run_id = %s),
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                handoff_to is None,
            ),
        )
        row = cur.fetchone()
        if row is None:
            # Slack retried a delivery it believed failed. Not an error.
            return {"recorded": False, "reason": "ALREADY_RECORDED", "context": context}

        if handoff_to is None:
            cur.execute(
                "UPDATE runs SET workflow_status = 'COMPLETED', updated_at = now() "
                "WHERE run_id = %s "
                "AND workflow_status IN ('AWAITING_TRIAGE', 'NOTIFY_PENDING', 'REVIEW_READY')",
                (run_id,),
            )
        else:
            cur.execute(
                "UPDATE runs SET triage_destination = %s, workflow_status = 'REVIEW_READY', "
                "updated_at = now() WHERE run_id = %s "
                "AND workflow_status IN ('AWAITING_TRIAGE', 'NOTIFY_PENDING', 'REVIEW_READY')",
                (handoff_to.value, run_id),
            )
        cur.execute(
            """
            INSERT INTO integration_events (event_id, correlation_id, run_id,
                event_type, event_status, source, occurred_at)
            VALUES (%s, (SELECT correlation_id FROM runs WHERE run_id = %s), %s,
                    %s, 'SUCCEEDED', 'SLACK_INBOUND', now())
            """,
            (
                uuid.uuid4(),
                run_id,
                run_id,
                "TRIAGE_DECISION_RECORDED" if handoff_to is None else "TRIAGE_HANDOFF_RECORDED",
            ),
        )

    return {
        "recorded": True,
        "decision_id": str(row[0]),
        "context": context,
        "handoff_to": handoff_to,
    }


def refresh_decided_card(
    conn: Connection, *, run_id: uuid.UUID, channel: str | None, message_ts: str | None, client
) -> bool:
    """Show the recorded final decision on a card someone clicked after it.

    A card keeps its buttons when the update after the decision was lost
    (ADR-007), and each click on it would be refused. The clicked card is
    updated from the final decision instead, so it stops offering a choice that
    has already been made. Best effort, like every card update.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT d.action, d.decided_by, d.decided_at, r.xero_invoice_number "
            "FROM triage_decisions d JOIN runs r USING (run_id) "
            "WHERE d.run_id = %s AND d.is_final",
            (run_id,),
        )
        decided = cur.fetchone()
    conn.commit()  # I12: the read is not held open across the Slack call
    if decided is None or not channel or not message_ts:
        return False
    action, decided_by, decided_at, number = decided
    outcome = client.update_card(
        channel=channel,
        message_ts=message_ts,
        blocks=build_decided_card(
            invoice_number=number,
            action=TriageAction(action),
            decided_by=decided_by,
            decided_at=decided_at.strftime("%d %b %Y, %H:%M UTC"),
        ),
        text=f"Bill {escape_mrkdwn(number)} triaged",
    )
    return outcome.ok


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
    conn.commit()  # I12: the read is not held open across the Slack call
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
            handed_to=HANDOFFS.get(action),
        ),
        text=f"Bill {escape_mrkdwn(context.invoice_number)} triaged",
    )
    if outcome.ok:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE slack_notifications SET card_updated_at = now() WHERE run_id = %s",
                (run_id,),
            )
    return outcome.ok
