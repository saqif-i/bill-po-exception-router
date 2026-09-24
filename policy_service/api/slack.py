"""Inbound Slack interactions.

Invariants I03 and I09.

This is the ONLY endpoint reachable from the public internet, so it does not use
the internal bearer token: Slack cannot send one. It is authenticated by the
request signature instead, which is why invariant I09 says a correlation id from
unauthenticated external ingress is never trusted.
"""

from __future__ import annotations

import json
import logging
import uuid

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from policy_service.config import get_settings
from policy_service.db.engine import get_pool
from policy_service.domain import triage
from policy_service.domain.enums import TriageAction
from policy_service.security.hmac_verify import verify

router = APIRouter(tags=["slack"])


@router.post("/slack/interactions")
async def interactions(request: Request) -> Response:
    """Slack requires a response within THREE SECONDS or it shows the user an
    error and retries.

    The work here is one short transaction, so it completes inside the budget
    and the acknowledgement is sent after it. Acknowledging first and writing
    afterwards would mean a crash between them loses a decision a person
    believes they made.

    If this ever grows past the budget, the correct change is to commit the
    decision, acknowledge, and move the CARD UPDATE to a background path. Never
    to acknowledge before the decision is durable.
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
        logging.getLogger(__name__).warning(
            "slack signature rejected", extra={"reason": failure_reason}
        )
        return JSONResponse(status_code=401, content={"error": "UNAUTHENTICATED"})

    try:
        form = dict(pair.split("=", 1) for pair in body.decode().split("&"))
        from urllib.parse import unquote_plus

        payload = json.loads(unquote_plus(form["payload"]))
    except Exception:  # a malformed body is a client fault, not a server one
        return JSONResponse(status_code=400, content={"error": "MALFORMED_PAYLOAD"})

    if payload.get("type") != "block_actions" or not payload.get("actions"):
        return PlainTextResponse("", status_code=200)

    chosen = payload["actions"][0]
    try:
        action = TriageAction(chosen.get("action_id"))
        run_id = uuid.UUID(chosen.get("value"))
    except (ValueError, TypeError):
        return JSONResponse(status_code=400, content={"error": "UNKNOWN_ACTION"})

    user = payload.get("user", {})
    # Slack does not send a dedicated interaction id, so one is derived from the
    # values that uniquely identify this click. A retry of the same delivery
    # produces the same string and the unique index refuses the second write.
    interaction_id = (f"{payload.get('trigger_id', '')}:{run_id}:{action.value}")[:200]
    message_ts = (payload.get("message") or {}).get("ts")

    from policy_service.integrations.slack_client import SlackClient

    client = SlackClient(bot_token=settings.slack_bot_token or "")

    with get_pool().connection() as conn:
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
        except triage.NotifyRefusedError as refused:
            conn.rollback()
            return JSONResponse(status_code=409, content={"error": str(refused)})
        # Committed BEFORE the acknowledgement.
        conn.commit()

        if result["recorded"]:
            from datetime import UTC, datetime

            triage.update_card_best_effort(
                conn,
                run_id=run_id,
                context=result["context"],
                action=action,
                user_id=user.get("id", "unknown"),
                decided_at=datetime.now(UTC).strftime("%d %b %Y, %H:%M UTC"),
                client=client,
            )
            conn.commit()

    # An empty 200 leaves the card as the update left it.
    return PlainTextResponse("", status_code=200)
