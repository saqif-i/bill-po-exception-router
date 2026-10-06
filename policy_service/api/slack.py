"""Inbound Slack interactions.

Invariants I03, I09 and I43.

This is the ONLY endpoint reachable from the public internet, so it does not use
the internal bearer token: Slack cannot send one. It is authenticated by the
request signature instead, which is why invariant I09 says a correlation id from
unauthenticated external ingress is never trusted.

Two kinds of payload arrive here, both signed the same way:

- `block_actions`, a click on a card. Most controls are recorded at once. The
  four that need a note (escalate, send back, request more information,
  information received) open a modal instead, and record nothing yet.
- `view_submission`, that modal submitted. The note is checked, and the decision
  is recorded exactly as a click is (ADR-010).
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from urllib.parse import unquote_plus

from fastapi import APIRouter, BackgroundTasks, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.concurrency import run_in_threadpool

from policy_service.config import get_settings
from policy_service.db.engine import get_pool
from policy_service.domain import triage
from policy_service.domain.enums import TriageAction
from policy_service.integrations.slack_blocks import (
    MODAL_CALLBACK,
    NOTE_BLOCK,
    NOTE_LIMIT,
    TEAM,
    build_note_modal,
)
from policy_service.security.hmac_verify import verify

router = APIRouter(tags=["slack"])
log = logging.getLogger(__name__)


@router.post("/slack/interactions")
async def interactions(request: Request, background_tasks: BackgroundTasks) -> Response:
    """Slack requires a response within THREE SECONDS or it shows the user an
    error.

    A decision is one short transaction, so it completes inside the budget and
    the acknowledgement is sent after it. Acknowledging first and writing
    afterwards would mean a crash between them loses a decision a person
    believes they made.

    A click is answered after its card update, as it always was. A modal
    submission is answered as soon as its decision commits, and the card work,
    which can be two Slack calls, runs after the acknowledgement. Never the
    decision itself.
    """
    settings = get_settings()
    if not settings.slack_signing_secret:
        return JSONResponse(status_code=503, content={"error": "SLACK_NOT_CONFIGURED"})

    # The RAW body. Re-serialising the JSON changes the bytes and the signature
    # stops matching.
    body = await request.body()
    ok, failure_reason = verify(
        signing_secret=settings.slack_signing_secret,
        timestamp=request.headers.get("x-slack-request-timestamp"),
        signature=request.headers.get("x-slack-signature"),
        body=body,
    )
    if not ok:
        # The reason goes to the log, never to the response. Telling a caller
        # which check failed helps them pass it next time.
        log.warning("slack signature rejected", extra={"reason": failure_reason})
        return JSONResponse(status_code=401, content={"error": "UNAUTHENTICATED"})

    try:
        form = dict(pair.split("=", 1) for pair in body.decode().split("&"))
        payload = json.loads(unquote_plus(form["payload"]))
    except Exception:  # a malformed body is a client fault, not a server one
        return JSONResponse(status_code=400, content={"error": "MALFORMED_PAYLOAD"})

    # Everything from here waits on the database or on Slack. Run on the event
    # loop, it would hold up every other request, and a modal that opens late
    # is a modal that does not open: its trigger expires three seconds after
    # the click.
    return await run_in_threadpool(_dispatch, payload, background_tasks)


def _dispatch(payload: dict, background_tasks: BackgroundTasks) -> Response:
    kind = payload.get("type")
    if kind == "block_actions" and payload.get("actions"):
        return _click(payload)
    if kind == "view_submission" and (payload.get("view") or {}).get("callback_id") == (
        MODAL_CALLBACK
    ):
        return _submission(payload, background_tasks)
    # Anything else records nothing. That includes a cancelled modal: Slack
    # sends nothing for it unless asked to, and nothing is asked for.
    return PlainTextResponse("", status_code=200)


def _slack_client():
    from policy_service.integrations.slack_client import SlackClient

    return SlackClient(bot_token=get_settings().slack_bot_token or "")


def _click(payload: dict) -> Response:
    chosen = payload["actions"][0]
    try:
        action = TriageAction(chosen.get("action_id"))
        run_id = uuid.UUID(chosen.get("value"))
    except (ValueError, TypeError):
        return JSONResponse(status_code=400, content={"error": "UNKNOWN_ACTION"})

    user = payload.get("user", {})
    message_ts = (payload.get("message") or {}).get("ts")
    channel_id = (payload.get("channel") or {}).get("id") or (payload.get("container") or {}).get(
        "channel_id"
    )

    with _slack_client() as client, get_pool().connection() as conn:
        if action in triage.NOTE_ACTIONS:
            return _open_modal(conn, client, payload, action, run_id, message_ts, channel_id)

        # Slack does not send a dedicated interaction id, so one is derived from
        # the values that uniquely identify this click. The same delivery
        # arriving twice produces the same string and the unique index refuses
        # the second write.
        interaction_id = (f"{payload.get('trigger_id', '')}:{run_id}:{action.value}")[:200]
        try:
            result = triage.record_decision(
                conn,
                run_id=run_id,
                action=action,
                user_id=user.get("id", "unknown"),
                user_name=user.get("username"),
                interaction_id=interaction_id,
                message_ts=message_ts,
            )
        except (triage.NotifyRefusedError, triage.DecisionRefusedError) as refused:
            return _refused(
                conn, client, refused, run_id=run_id, channel=channel_id, message_ts=message_ts
            )
        # Committed BEFORE the acknowledgement.
        conn.commit()

        if result["recorded"]:
            _finish(
                conn,
                client,
                run_id=run_id,
                action=action,
                user_id=user.get("id", "unknown"),
                result=result,
            )

    # An empty 200 leaves the card as the update left it.
    return PlainTextResponse("", status_code=200)


def _open_modal(
    conn,
    client,
    payload: dict,
    action: TriageAction,
    run_id: uuid.UUID,
    message_ts: str | None,
    channel_id: str | None,
) -> Response:
    """Ask for the note. The same checks a decision faces come first, so nobody
    writes a reason the server would then refuse, and nothing is recorded:
    cancelling the modal leaves the case exactly as it was."""
    try:
        context = triage.check_click(conn, run_id=run_id, action=action, message_ts=message_ts)
    except (triage.NotifyRefusedError, triage.DecisionRefusedError) as refused:
        return _refused(
            conn, client, refused, run_id=run_id, channel=channel_id, message_ts=message_ts
        )
    conn.rollback()  # I12: nothing is held open while Slack is called

    returns_to = triage.handoff_destination(action, context)
    outcome = client.open_view(
        trigger_id=payload.get("trigger_id", ""),
        view=build_note_modal(
            action,
            run_id=str(run_id),
            invoice_number=context.invoice_number,
            message_ts=message_ts,
            channel=channel_id,
            team=TEAM[returns_to] if returns_to is not None else "",
        ),
    )
    if not outcome.ok:
        # Slack's reason is the diagnosis: expired_trigger_id is a slow
        # response, invalid_auth a bad token. docs/runbook.md section 7.
        log.warning(
            "modal not opened",
            extra={"run_id": str(run_id), "action": action.value, "slack_error": outcome.error},
        )
        return JSONResponse(status_code=502, content={"error": "MODAL_NOT_OPENED"})
    return PlainTextResponse("", status_code=200)


def _note_error(message: str) -> Response:
    """Shown under the note in the modal, which stays open to be corrected."""
    return JSONResponse(
        status_code=200, content={"response_action": "errors", "errors": {NOTE_BLOCK: message}}
    )


def _submission(payload: dict, background_tasks: BackgroundTasks) -> Response:
    view = payload["view"]
    try:
        metadata = json.loads(view.get("private_metadata") or "")
        action = TriageAction(metadata["action"])
        run_id = uuid.UUID(metadata["run_id"])
        view_id = str(view["id"])
        if action not in triage.NOTE_ACTIONS:
            raise ValueError(action)
    except (ValueError, TypeError, KeyError):
        return JSONResponse(status_code=400, content={"error": "MALFORMED_PAYLOAD"})

    # I43: bounded here, so the person can correct it, and again by the domain
    # and a CHECK. Trimmed first: whitespace is not a reason.
    values = (view.get("state") or {}).get("values") or {}
    note = ((values.get(NOTE_BLOCK) or {}).get(NOTE_BLOCK) or {}).get("value") or ""
    note = note.strip()
    if not note:
        return _note_error("Write a note before submitting.")
    if len(note) > NOTE_LIMIT:
        return _note_error(f"That is {len(note)} characters. The limit is {NOTE_LIMIT}.")

    user = payload.get("user", {})
    message_ts = metadata.get("message_ts")
    channel = metadata.get("channel")
    with _slack_client() as client, get_pool().connection() as conn:
        try:
            result = triage.record_decision(
                conn,
                run_id=run_id,
                action=action,
                user_id=user.get("id", "unknown"),
                user_name=user.get("username"),
                # One modal is one view id. A submission that arrives twice, a
                # second click on Submit after a slow answer, is recorded once.
                interaction_id=f"view:{view_id}:{action.value}"[:200],
                message_ts=message_ts,
                note=note,
            )
        except (triage.NotifyRefusedError, triage.DecisionRefusedError) as refused:
            return _refused(
                conn, client, refused, run_id=run_id, channel=channel, message_ts=message_ts
            )
        # Committed BEFORE the acknowledgement.
        conn.commit()

    if result["recorded"]:
        background_tasks.add_task(
            _finish_later,
            run_id=run_id,
            action=action,
            user_id=user.get("id", "unknown"),
            result=result,
        )
    # An empty 200 closes the modal.
    return PlainTextResponse("", status_code=200)


def _refused(
    conn,
    client,
    refused: Exception,
    *,
    run_id: uuid.UUID,
    channel: str | None,
    message_ts: str | None,
) -> Response:
    conn.rollback()
    reason = str(refused)
    if reason == "WORKFLOW_STATUS_COMPLETED":
        # Already decided: the card should not still have buttons. It is
        # refreshed from the recorded decision, and the click is answered with a
        # 200, since nothing about it is an error to show.
        triage.refresh_decided_card(
            conn, run_id=run_id, channel=channel, message_ts=message_ts, client=client
        )
        return PlainTextResponse("", status_code=200)
    if (
        isinstance(refused, triage.DecisionRefusedError)
        and reason != "CARD_SUPERSEDED"
        and message_ts
    ):
        # The current card offered something the case no longer allows: its
        # last update was lost. It is redrawn with the controls that apply, and
        # the click is still refused.
        triage.refresh_current_card(conn, run_id=run_id, message_ts=message_ts, client=client)
        conn.commit()
    return JSONResponse(status_code=409, content={"error": reason})


def _finish(
    conn, client, *, run_id: uuid.UUID, action: TriageAction, user_id: str, result: dict
) -> None:
    """The card work once a decision has committed. Best effort (ADR-007).

    The clicked card shows the decision, or for a request or an answer is
    redrawn in place. After a hand-off the case continues with the other team,
    so their card is posted now. If that fails the run waits in REVIEW_READY,
    and the poll re-sends it through workflow 02 after 15 minutes.
    """
    triage.update_card_best_effort(
        conn,
        run_id=run_id,
        context=result["context"],
        action=action,
        user_id=user_id,
        decided_at=triage.stamp(datetime.now(UTC)),
        client=client,
        note=result.get("note"),
    )
    conn.commit()

    if result.get("handoff_to") is not None:
        try:
            triage.notify(conn, run_id=run_id, correlation_id=uuid.uuid4(), client=client)
            conn.commit()
        except Exception:
            conn.rollback()
            log.warning(
                "hand-off card not posted; left for the re-send",
                extra={"run_id": str(run_id)},
                exc_info=True,
            )


def _finish_later(*, run_id: uuid.UUID, action: TriageAction, user_id: str, result: dict) -> None:
    """_finish, after a modal submission has been acknowledged. Runs with its
    own connection and client, because the request's are closed by then, and
    nothing it raises may escape: the decision is already recorded."""
    try:
        with _slack_client() as client, get_pool().connection() as conn:
            _finish(conn, client, run_id=run_id, action=action, user_id=user_id, result=result)
    except Exception:
        log.warning(
            "card work after a submission failed",
            extra={"run_id": str(run_id), "action": action.value},
            exc_info=True,
        )
