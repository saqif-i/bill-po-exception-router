"""Notification and the human decision.

Invariants I03, I12, I31, I42 and I43.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from psycopg import Connection

from policy_service.domain.enums import TriageAction, TriageDestination
from policy_service.integrations.slack_blocks import (
    ACTIONS_FOR,
    CHANNEL_FOR,
    NOTE_LIMIT,
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
# Send back is a hand-off too, to the team that escalated the case, so its
# destination depends on the case: see handoff_destination().
HANDOFFS: dict[TriageAction, TriageDestination] = {
    TriageAction.SEND_TO_FINANCE: TriageDestination.FINANCE,
    TriageAction.SEND_TO_PROCUREMENT: TriageDestination.PROCUREMENT,
    TriageAction.ESCALATE: TriageDestination.ESCALATED,
}

# The controls that open a modal and are recorded with the note typed into it:
# the escalation reason, the send-back note, the question and the answer.
NOTE_ACTIONS: frozenset[TriageAction] = frozenset(
    {
        TriageAction.ESCALATE,
        TriageAction.SEND_BACK,
        TriageAction.REQUEST_MORE_INFORMATION,
        TriageAction.INFORMATION_RECEIVED,
    }
)

# Recorded on the current card, which stays where it is: no new destination
# and no new card.
IN_PLACE: frozenset[TriageAction] = frozenset(
    {TriageAction.REQUEST_MORE_INFORMATION, TriageAction.INFORMATION_RECEIVED}
)

# Never close a run. The `triage_handoff_is_not_final` CHECK in migration 005c
# lists the same actions, and a test keeps the two in step.
NON_FINAL: frozenset[TriageAction] = frozenset(HANDOFFS) | NOTE_ACTIONS

_EVENT_TYPES: dict[TriageAction, str] = {
    TriageAction.SEND_TO_FINANCE: "TRIAGE_HANDOFF_RECORDED",
    TriageAction.SEND_TO_PROCUREMENT: "TRIAGE_HANDOFF_RECORDED",
    TriageAction.ESCALATE: "TRIAGE_ESCALATION_RECORDED",
    TriageAction.SEND_BACK: "TRIAGE_SENT_BACK",
    TriageAction.REQUEST_MORE_INFORMATION: "TRIAGE_INFORMATION_REQUESTED",
    TriageAction.INFORMATION_RECEIVED: "TRIAGE_INFORMATION_RECEIVED",
}

_DECIDABLE_STATUSES = ("AWAITING_TRIAGE", "NOTIFY_PENDING", "REVIEW_READY")


def stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%d %b %Y, %H:%M UTC")


def allowed_actions(
    destination: TriageDestination, *, information_open: bool, escalated: bool
) -> tuple[TriageAction, ...]:
    """The controls a case offers now. Cards are built from this and clicks are
    checked against it, so a card never shows a control the server refuses.

    The destination's set, less Escalate once the case has been escalated (I42),
    which also keeps it off the card a send-back creates. While an information
    request is open, only "Information received", and Escalate where it would
    otherwise be offered: a team waiting on an answer can still escalate.
    """
    offered = tuple(
        action
        for action in ACTIONS_FOR[destination]
        if not (escalated and action is TriageAction.ESCALATE)
    )
    if information_open:
        return (
            TriageAction.INFORMATION_RECEIVED,
            *(action for action in offered if action is TriageAction.ESCALATE),
        )
    return offered


@dataclass
class CardContext:
    run_id: uuid.UUID
    invoice_number: str
    destination: TriageDestination
    exception_codes: list[str]
    recommendation: dict | None
    semantic_gate_reason: str
    human_review_reasons: list[str]
    # Who handed the case to this destination, if anyone: the latest hand-off,
    # with its note when it was an escalation or a send-back.
    handed_over: dict | None = None
    po_number: str | None = None
    # Every decision recorded so far, oldest first.
    history: list[dict] = field(default_factory=list)
    # The open information request ({"question", "by", "at"}), if any.
    information_open: dict | None = None
    # The team that escalated the case, once it has been: Send back returns it there.
    escalated_from: TriageDestination | None = None

    @property
    def actions(self) -> tuple[TriageAction, ...]:
        return allowed_actions(
            self.destination,
            information_open=self.information_open is not None,
            escalated=self.escalated_from is not None,
        )


@dataclass
class _History:
    entries: list[dict]
    handed_over: dict | None
    information_open: dict | None
    escalated_from: TriageDestination | None


def _read_history(rows: list[tuple]) -> _History:
    """The case so far, from its decision rows in the order they were made.

    A request is open while the requests outnumber the answers. Counting, rather
    than looking at the latest row, holds because only one request can be open
    at a time. It counts across destinations: a question asked by finance stays
    open after finance escalates, and the escalation card shows it.

    Only non-final rows are hand-offs, requests or escalations. A final Escalate
    or Request more information row was recorded before migration 005c, when
    both closed the run, and is history only.
    """
    entries: list[dict] = []
    handed_over = None
    escalated_from = None
    question = None
    asked = answered = 0
    for action, decided_by, decided_at, shown, note, is_final in rows:
        action = TriageAction(action)
        source = TriageDestination(shown)
        to = None
        if not is_final and action is TriageAction.SEND_BACK:
            to = escalated_from
        elif not is_final and action in HANDOFFS:
            to = HANDOFFS[action]
        if not is_final and action is TriageAction.ESCALATE:
            escalated_from = source
        entry = {
            "action": action,
            "by": decided_by,
            "at": stamp(decided_at),
            "from": source,
            "to": to,
            "note": note,
        }
        entries.append(entry)
        if to is not None:
            handed_over = entry
        if not is_final and action is TriageAction.REQUEST_MORE_INFORMATION:
            asked += 1
            question = entry
        elif action is TriageAction.INFORMATION_RECEIVED:
            answered += 1
    information_open = (
        {"question": question["note"], "by": question["by"], "at": question["at"]}
        if question is not None and asked > answered
        else None
    )
    return _History(entries, handed_over, information_open, escalated_from)


def _load_history(cur, run_id: uuid.UUID, *, include_final: bool = True) -> _History:
    """`include_final=False` leaves out the closing decision, which a decided
    card shows on its own line."""
    cur.execute(
        "SELECT action, decided_by, decided_at, shown_destination, note, is_final "
        "FROM triage_decisions WHERE run_id = %s ORDER BY decided_at, decision_id",
        (run_id,),
    )
    return _read_history([row for row in cur.fetchall() if include_final or not row[5]])


def load_card_context(
    conn: Connection,
    run_id: uuid.UUID,
    *,
    allowed_statuses: tuple[str, ...],
    lock: bool = True,
) -> CardContext:
    """Everything the card shows, with a state check the caller chooses.

    Notification demands REVIEW_READY (invariant I31). Recording a decision must
    also accept AWAITING_TRIAGE, because notification itself moved the run
    there: a reviewer clicking a button they were just shown is the normal path,
    not a violation.

    `lock=False` is for a preview that records nothing, such as the check made
    before a modal opens. Whatever is then recorded is checked again under the
    lock.
    """
    with conn.cursor() as cur:
        if lock:
            cur.execute("SELECT 1 FROM runs WHERE run_id = %s FOR UPDATE", (run_id,))
        cur.execute(
            """
            SELECT r.workflow_status, r.reconciliation_outcome, r.semantic_stage_status,
                   r.triage_destination, r.xero_invoice_number, r.human_review_reasons,
                   rr.semantic_gate_reason, rr.po_number
              FROM runs r JOIN reconciliation_results rr USING (run_id)
             WHERE r.run_id = %s
            """,
            (run_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise NotifyRefusedError("RUN_NOT_FOUND")
        (status, outcome, stage, destination, number, reasons, gate_reason, po_number) = row

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

        history = _load_history(cur, run_id)

    return CardContext(
        run_id=run_id,
        invoice_number=number,
        destination=TriageDestination(destination),
        exception_codes=codes,
        recommendation=recommendation,
        semantic_gate_reason=gate_reason,
        human_review_reasons=list(reasons),
        handed_over=history.handed_over,
        po_number=po_number,
        history=history.entries,
        information_open=history.information_open,
        escalated_from=history.escalated_from,
    )


def _card(context: CardContext) -> list[dict]:
    return build_card(
        run_id=str(context.run_id),
        invoice_number=context.invoice_number,
        destination=context.destination,
        exception_codes=context.exception_codes,
        recommendation=context.recommendation,
        semantic_gate_reason=context.semantic_gate_reason,
        human_review_reasons=context.human_review_reasons,
        handed_over=context.handed_over,
        actions=context.actions,
        po_number=context.po_number,
        history=context.history,
        waiting=context.information_open,
    )


def awaits_card(conn: Connection, run_id: uuid.UUID) -> bool:
    """True when the run is ready for a card that its current destination has
    not been sent: after a hand-off, an escalation or a send-back, the card on
    record is the previous team's."""
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

    blocks = _card(context)
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


def handoff_destination(action: TriageAction, context: CardContext) -> TriageDestination | None:
    """Where a hand-off sends the case. Send back returns it to the team that
    escalated it."""
    if action is TriageAction.SEND_BACK:
        return context.escalated_from
    return HANDOFFS.get(action)


def _refusal(action: TriageAction, context: CardContext) -> str | None:
    """Why the case does not allow this action now, or None if it does."""
    if action in context.actions:
        return None
    if action is TriageAction.INFORMATION_RECEIVED:
        return "NO_INFORMATION_REQUEST_OPEN"
    if action is TriageAction.ESCALATE and context.escalated_from is not None:
        return "ALREADY_ESCALATED"
    if action not in ACTIONS_FOR[context.destination]:
        return f"ACTION_NOT_OFFERED_FOR_{context.destination.value}"
    return "INFORMATION_REQUEST_OPEN"


def _check_click(
    conn: Connection,
    run_id: uuid.UUID,
    action: TriageAction,
    message_ts: str | None,
    *,
    lock: bool,
) -> CardContext:
    """The card clicked must be the current one, and the case must allow the
    action now. Raises DecisionRefusedError, or NotifyRefusedError for a run in
    the wrong state.

    The card is checked FIRST. A click on a card a hand-off replaced is refused
    as superseded whatever the control: checked second, an Escalate from the
    old finance card would be refused as "not offered for escalations", which
    is true of a card it did not come from.
    """
    # NOTIFY_PENDING too: a click can only come from a card Slack has posted,
    # and it can arrive before notify has recorded that post.
    context = load_card_context(conn, run_id, allowed_statuses=_DECIDABLE_STATUSES, lock=lock)
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
    refusal = _refusal(action, context)
    if refusal is not None:
        raise DecisionRefusedError(refusal)
    return context


def check_click(
    conn: Connection, *, run_id: uuid.UUID, action: TriageAction, message_ts: str | None
) -> CardContext:
    """The checks a decision would face, made before a modal opens so a person
    is not asked for a note the server would then refuse. Records nothing and
    takes no lock; the submission is checked again, under the lock, when it is
    recorded. The caller ends the transaction before calling Slack (I12)."""
    return _check_click(conn, run_id, action, message_ts, lock=False)


def _checked_note(action: TriageAction, note: str | None) -> str | None:
    """I43. The handler validates first so the person can correct the modal;
    this is the same rule where the decision is made."""
    if action not in NOTE_ACTIONS:
        return None
    note = (note or "").strip()
    if not note:
        raise DecisionRefusedError("NOTE_REQUIRED")
    if len(note) > NOTE_LIMIT:
        raise DecisionRefusedError("NOTE_TOO_LONG")
    return note


def record_decision(
    conn: Connection,
    *,
    run_id: uuid.UUID,
    action: TriageAction,
    user_id: str,
    user_name: str | None,
    interaction_id: str,
    message_ts: str | None,
    note: str | None = None,
) -> dict:
    """The decision, in ONE transaction, committed BEFORE Slack is acknowledged.

    Invariant I11, at its reduced surface: the XERO_HISTORY_NOTE half of the
    original transaction has no subject in this build, but the ordering that
    matters is unchanged. Acknowledging first and writing afterwards would mean
    a crash between them loses a decision a person believes they made.

    Three kinds of decision (ADR-009, ADR-010):

    - A hand-off (send to finance or procurement, escalate, send back) does not
      close the run: the run moves to the new destination and back to
      REVIEW_READY, so notify posts that team's card, and the result carries
      `handoff_to` so the caller can post it at once.
    - A request for information, or its answer, is recorded on the current
      card. The run stays where it is, and the result carries `in_place`.
    - Anything else is final and closes the run. One per run.

    Only the current card's controls count, and only those the case allows now:
    see _check_click().
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

    context = _check_click(conn, run_id, action, message_ts, lock=True)
    note = _checked_note(action, note)
    handoff_to = handoff_destination(action, context)
    if action is TriageAction.SEND_BACK and handoff_to is None:
        raise DecisionRefusedError("NOT_ESCALATED")
    is_final = action not in NON_FINAL

    with conn.cursor() as cur:
        # clock_timestamp(), not the default now(): now() is when this
        # transaction began, before it waited for the run's lock, and the order
        # of these rows is the case's history.
        cur.execute(
            """
            INSERT INTO triage_decisions (decision_id, run_id, correlation_id,
                action, decided_by, decided_by_name, decided_at, shown_exception_codes,
                shown_destination, shown_recommendation, shown_confidence,
                shown_gate_reason, slack_message_ts, slack_interaction_id, is_final, note)
            VALUES (%s, %s, (SELECT correlation_id FROM runs WHERE run_id = %s),
                    %s, %s, %s, clock_timestamp(), %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                is_final,
                note,
            ),
        )
        row = cur.fetchone()
        if row is None:
            # Slack retried a delivery it believed failed. Not an error.
            return {"recorded": False, "reason": "ALREADY_RECORDED", "context": context}

        if is_final:
            cur.execute(
                "UPDATE runs SET workflow_status = 'COMPLETED', updated_at = now() "
                "WHERE run_id = %s AND workflow_status = ANY(%s)",
                (run_id, list(_DECIDABLE_STATUSES)),
            )
        elif handoff_to is not None:
            cur.execute(
                "UPDATE runs SET triage_destination = %s, workflow_status = 'REVIEW_READY', "
                "updated_at = now() WHERE run_id = %s AND workflow_status = ANY(%s)",
                (handoff_to.value, run_id, list(_DECIDABLE_STATUSES)),
            )
        else:
            cur.execute("UPDATE runs SET updated_at = now() WHERE run_id = %s", (run_id,))
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
                _EVENT_TYPES.get(action, "TRIAGE_DECISION_RECORDED"),
            ),
        )

    return {
        "recorded": True,
        "decision_id": str(row[0]),
        "context": context,
        "handoff_to": handoff_to,
        "in_place": action in IN_PLACE,
        "note": note,
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
            "SELECT d.action, d.decided_by, d.decided_at, d.note, r.xero_invoice_number "
            "FROM triage_decisions d JOIN runs r USING (run_id) "
            "WHERE d.run_id = %s AND d.is_final",
            (run_id,),
        )
        decided = cur.fetchone()
        history = _load_history(cur, run_id, include_final=False)
    conn.commit()  # I12: the read is not held open across the Slack call
    if decided is None or not channel or not message_ts:
        return False
    action, decided_by, decided_at, note, number = decided
    outcome = client.update_card(
        channel=channel,
        message_ts=message_ts,
        blocks=build_decided_card(
            invoice_number=number,
            action=TriageAction(action),
            decided_by=decided_by,
            decided_at=stamp(decided_at),
            note=note,
            history=history.entries,
        ),
        text=f"Bill {escape_mrkdwn(number)} triaged",
    )
    return outcome.ok


def refresh_current_card(
    conn: Connection,
    *,
    run_id: uuid.UUID,
    client,
    message_ts: str | None = None,
) -> bool:
    """Re-draw the run's current card with the controls its state allows now.

    After a request for information or its answer, this is the card update: the
    card stays where it is and changes what it offers. After a refused click on
    the current card it is the repair. A lost update (ADR-007) would otherwise
    leave a card waiting on an answer with no "Information received" button,
    and nothing anyone could click would move the case.

    With `message_ts`, only when that is the current card: the card a hand-off
    replaced is never redrawn as the new one. Best effort; no transaction is
    open during the Slack call (I12).
    """
    try:
        context = load_card_context(conn, run_id, allowed_statuses=("AWAITING_TRIAGE",), lock=False)
    except NotifyRefusedError:
        # Decided meanwhile, or not on a posted card: nothing to redraw.
        conn.rollback()
        return False
    with conn.cursor() as cur:
        cur.execute(
            "SELECT channel, message_ts, post_status, destination FROM slack_notifications "
            "WHERE run_id = %s",
            (run_id,),
        )
        card = cur.fetchone()
    conn.commit()
    if (
        card is None
        or card[2] != "POSTED"
        or card[3] != context.destination.value
        or not card[1]
        or (message_ts is not None and message_ts != card[1])
    ):
        return False
    outcome = client.update_card(
        channel=card[0],
        message_ts=card[1],
        blocks=_card(context),
        text=f"Bill {escape_mrkdwn(context.invoice_number)} needs review",
    )
    if outcome.ok:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE slack_notifications SET card_updated_at = now() WHERE run_id = %s",
                (run_id,),
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
    note: str | None = None,
) -> bool:
    """After the decision commits. The database is authoritative; this is a view.

    A lost response leaves the card stale while the decision is correctly
    recorded, which is ADR-007 and is not claimed to be otherwise.

    `context` is the case as it was before this decision. A request or answer
    redraws the current card in place; anything else replaces its buttons with
    the decision, and for a hand-off where the case went.
    """
    if action in IN_PLACE:
        return refresh_current_card(conn, run_id=run_id, client=client)

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
            handed_to=handoff_destination(action, context),
            note=note,
            history=context.history,
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
