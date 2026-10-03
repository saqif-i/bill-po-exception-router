"""Failure alerts from the n8n error workflow.

n8n holds no Slack token (ADR-002), so workflow 03 cannot post an alert itself.
It calls this endpoint, and the service posts with the one Slack client it
already has (ADR-004). Without this, a failed bill showed up only in n8n's
execution list, which nobody watches.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from policy_service.api.auth import require_internal_bearer
from policy_service.api.errors import ServiceError
from policy_service.api.idempotency import canonical_hash, scope, validate_key
from policy_service.api.runs import _body, _release_claim
from policy_service.config import get_settings
from policy_service.db import idempotency_store
from policy_service.db.engine import get_pool
from policy_service.integrations.slack_blocks import ALERTS_CHANNEL, build_alert, escape_mrkdwn
from policy_service.security.redaction import redact

router = APIRouter(prefix="/alerts", tags=["alerts"])


class AlertBody(BaseModel):
    """What workflow 03 knows about the failure. Every field is bounded."""

    workflow: str = Field(max_length=200)
    execution_id: str | None = Field(default=None, max_length=100)
    failed_node: str | None = Field(default=None, max_length=200)
    message: str | None = Field(default=None, max_length=2000)
    failed_at: str | None = Field(default=None, max_length=64)


@router.post("")
def alert(
    body: AlertBody,
    request: Request,
    principal: str = Depends(require_internal_bearer),
) -> JSONResponse:
    """Post one failure to the alerts channel. Idempotent per key (I36)."""
    from policy_service.integrations.slack_client import SlackClient

    key = validate_key(request)
    operation = "alerts"
    correlation_id = uuid.UUID(request.state.correlation_id)
    request_hash = canonical_hash(principal, operation, body.model_dump())
    settings = get_settings()

    if not settings.slack_bot_token:
        raise ServiceError(code="SLACK_NOT_CONFIGURED", status_code=503)

    with get_pool().connection() as conn:
        claim = idempotency_store.claim(
            conn,
            scope=scope(principal, operation),
            key=key,
            request_hash=request_hash,
            correlation_id=correlation_id,
            owner_id=principal,
        )
        if claim.replayed:
            return JSONResponse(
                status_code=claim.result_status or 200,
                content=_body(request, dict(claim.result_summary or {})),
                headers={"Idempotency-Replayed": "true"},
            )
        conn.commit()

    # The message can quote a provider response, so it is scrubbed for anything
    # credential-shaped before it reaches a channel.
    message = redact(body.message) if body.message else None
    try:
        with SlackClient(bot_token=settings.slack_bot_token) as slack:
            outcome = slack.post_card(
                channel=ALERTS_CHANNEL,
                blocks=build_alert(
                    workflow=body.workflow,
                    failed_node=body.failed_node,
                    message=message,
                    execution_id=body.execution_id,
                    failed_at=body.failed_at,
                ),
                text=f"Workflow {escape_mrkdwn(body.workflow)} failed",
            )
    except Exception:
        _release_claim(claim, "ALERT_FAILED")
        raise

    summary = {"posted": outcome.ok, "channel": ALERTS_CHANNEL, "error": outcome.error or None}
    if not outcome.ok:
        # An alert nobody received is the failure this endpoint exists to stop.
        _release_claim(claim, "ALERT_NOT_POSTED")
        return JSONResponse(
            status_code=502, content=_body(request, {**summary, "error": "ALERT_NOT_POSTED"})
        )

    with get_pool().connection() as conn:
        idempotency_store.complete(
            conn, claim.ledger_id, generation=claim.generation, status_code=200, summary=summary
        )
        conn.commit()
    return JSONResponse(status_code=200, content=_body(request, summary))
