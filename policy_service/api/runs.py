"""The run endpoints.

n8n calls these. It holds one bearer token and nothing else: no database
credential, no Xero credential (ADR-002). Correctness lives here and in
PostgreSQL, never in n8n ordering or concurrency settings (I31).

Every mutating endpoint validates its `Idempotency-Key` BEFORE any domain
mutation, so a malformed key cannot leave a half-finished run behind.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from policy_service.api.auth import require_internal_bearer
from policy_service.api.errors import ServiceError
from policy_service.api.idempotency import (
    canonical_hash,
    scope,
    validate_key,
)
from policy_service.config import get_settings
from policy_service.db import idempotency_store
from policy_service.db.engine import get_pool
from policy_service.domain import poller
from policy_service.domain.models import Bill, PurchaseOrder
from policy_service.integrations.xero_client import XeroApiError

router = APIRouter(prefix="/runs", tags=["runs"])

MAX_BILLS_PER_POLL = 50
# Pages read per poll. A backlog larger than this is read over several polls.
MAX_POLL_PAGES = 20
POLL_OVERLAP_MINUTES = 5


def _body(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    return {**payload, "correlation_id": getattr(request.state, "correlation_id", None)}


def _release_claim(ledger_id: uuid.UUID, error_code: str) -> None:
    """Record a failed attempt as RETRYABLE so the same key can run again.

    Written on a fresh connection because the request's own transaction has just
    been rolled back. Without this the claim stays PROCESSING, and every retry
    with the same key gets IN_PROGRESS until the lease expires.
    """
    with get_pool().connection() as other:
        idempotency_store.fail(other, ledger_id, error_class="RETRYABLE", error_code=error_code)
        other.commit()


@router.post("/poll")
def poll(request: Request, principal: str = Depends(require_internal_bearer)) -> JSONResponse:
    """Ingest draft ACCPAY bills modified since the watermark.

    The service owns the watermark. n8n sends no cursor and no cutoff, because a
    client-supplied cursor is a client-supplied opportunity to skip a bill.

    Pages are read until one comes back short. A complete poll moves the cursor
    to when it started. A poll that hits the page cap moves it only as far as
    the last bill it received, and a failed poll leaves it alone, so neither
    skips a bill.
    """
    from datetime import UTC, datetime

    from policy_service.api.deps import get_xero_client

    key = validate_key(request)
    operation = "runs-poll"
    correlation_id = uuid.UUID(request.state.correlation_id)
    request_hash = canonical_hash(principal, operation, {})

    client = get_xero_client()
    if client is None:
        raise ServiceError(code="XERO_NOT_CONFIGURED", status_code=503)

    started_at = datetime.now(UTC)
    report = poller.PollReport()

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

        try:
            mark = poller.read_cursor(conn)
            fixtures = poller.active_fixtures(conn)
            # I12: nothing stays open while Xero is paged. Every page is read
            # first, then the runs and the cursor are written in one transaction.
            conn.commit()

            def fetch_page(page: int) -> list[dict]:
                payload = client.list_invoices(
                    modified_since=mark.isoformat() if mark else None,
                    max_records=MAX_BILLS_PER_POLL,
                    page=page,
                )
                return payload.get("Invoices", [])

            received: list[dict] = []
            walk = poller.walk_pages(
                fetch_page, received.append, page_size=MAX_BILLS_PER_POLL, max_pages=MAX_POLL_PAGES
            )
            for raw in received:
                report.polled += 1
                bill = Bill.model_validate(raw)
                run_id, reason = poller.ingest_bill(conn, bill, fixtures, correlation_id)
                if run_id is not None:
                    report.ingested += 1
                    report.run_ids.append(str(run_id))
                elif reason == "NOT_ON_ACTIVE_ALLOW_LIST":
                    report.skipped_not_allow_listed += 1
                else:
                    report.skipped_unchanged += 1

            truncated = walk.truncated
            if not truncated:
                poller.advance_cursor(conn, started_at, POLL_OVERLAP_MINUTES)
                report.cursor_advanced = True
            elif walk.last_updated is not None:
                # Everything up to the last bill received has been read. The
                # rest is picked up next poll.
                poller.advance_cursor(conn, walk.last_updated, POLL_OVERLAP_MINUTES)
                report.cursor_advanced = True
            summary = {
                "polled": report.polled,
                "ingested": report.ingested,
                "skipped_not_allow_listed": report.skipped_not_allow_listed,
                "skipped_unchanged": report.skipped_unchanged,
                "truncated": truncated,
                "run_ids": report.run_ids,
            }
            idempotency_store.complete(conn, claim.ledger_id, status_code=200, summary=summary)
            conn.commit()
        except Exception:
            conn.rollback()
            _release_claim(claim.ledger_id, "POLL_FAILED")
            raise

    return JSONResponse(status_code=200, content=_body(request, summary))


@router.post("/{run_id}/reconcile")
def reconcile_run(
    run_id: uuid.UUID,
    request: Request,
    principal: str = Depends(require_internal_bearer),
) -> JSONResponse:
    """Run the deterministic engine and persist the decision atomically."""
    from policy_service.api.deps import get_xero_client

    key = validate_key(request)
    operation = "runs-reconcile"
    correlation_id = uuid.UUID(request.state.correlation_id)
    request_hash = canonical_hash(principal, operation, {"run_id": str(run_id)})

    client = get_xero_client()
    if client is None:
        raise ServiceError(code="XERO_NOT_CONFIGURED", status_code=503)

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

        # Any failure below leaves the run in INGESTED and the key reclaimable.
        # Without this the claim would stay PROCESSING, and every retry with the
        # same key would get IN_PROGRESS for good.
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT xero_invoice_id, xero_invoice_number FROM runs WHERE run_id = %s",
                    (run_id,),
                )
                row = cur.fetchone()
            # I12: nothing stays open across the Xero calls below. Everything
            # is read first, then the decision is written in one transaction.
            conn.commit()
            if row is None:
                raise ServiceError(code="RUN_NOT_FOUND", status_code=404)

            match = client.get_invoice(str(row[0]))
            if match is None:
                raise ServiceError(code="BILL_NO_LONGER_AVAILABLE", status_code=409)

            bill = Bill.model_validate(match)
            from policy_service.domain.normalisation import split_bill_reference

            reference = split_bill_reference(bill.invoice_number)[1]
            purchase_order = None
            if reference:
                order = client.find_purchase_order(reference)
                if order is not None:
                    purchase_order = PurchaseOrder.model_validate(order)
            chart = client.chart_of_accounts()

            summary = poller.reconcile_run(
                conn,
                run_id=run_id,
                correlation_id=correlation_id,
                bill=bill,
                purchase_order=purchase_order,
                po_allow_listed=poller.purchase_order_allow_listed(conn, purchase_order),
                chart=chart,
                semantic_review_enabled=get_settings().semantic_review_enabled,
            )
            idempotency_store.complete(conn, claim.ledger_id, status_code=200, summary=summary)
            conn.commit()
        except Exception as exc:
            conn.rollback()
            _release_claim(claim.ledger_id, "RECONCILE_FAILED")
            if isinstance(exc, XeroApiError | httpx.HTTPError):
                raise ServiceError(code="XERO_UNAVAILABLE", status_code=503) from exc
            raise

    return JSONResponse(status_code=200, content=_body(request, summary))


@router.post("/{run_id}/semantic-review")
def semantic_review(
    run_id: uuid.UUID,
    request: Request,
    principal: str = Depends(require_internal_bearer),
) -> JSONResponse:
    """Ask for a bounded recommendation on wording, if the gate permits it.

    Every path here ends with the run reaching a human. There is no branch in
    which a recommendation replaces a review.
    """
    from policy_service.domain import semantic
    from policy_service.integrations.claude_client import ClaudeClient

    key = validate_key(request)
    operation = "runs-semantic-review"
    correlation_id = uuid.UUID(request.state.correlation_id)
    request_hash = canonical_hash(principal, operation, {"run_id": str(run_id)})
    settings = get_settings()

    if not settings.anthropic_api_key:
        raise ServiceError(code="SEMANTIC_NOT_CONFIGURED", status_code=503)

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

        client = ClaudeClient(
            api_key=settings.anthropic_api_key,
            model=settings.semantic_model_id,
            timeout_seconds=settings.semantic_timeout_seconds,
        )
        try:
            try:
                summary = semantic.review(
                    conn,
                    run_id=run_id,
                    correlation_id=correlation_id,
                    client=client,
                    min_confidence=settings.semantic_min_confidence,
                    enabled_flag=settings.semantic_review_enabled,
                )
            except semantic.GateRefusedError as refused:
                # Not an error. The gate doing its job, recorded and returned, so
                # n8n carries on to notification rather than treating it as failure.
                summary = {"applied": False, "gate_refused": str(refused)}

            idempotency_store.complete(conn, claim.ledger_id, status_code=200, summary=summary)
            conn.commit()
        except Exception:
            conn.rollback()
            _release_claim(claim.ledger_id, "SEMANTIC_REVIEW_FAILED")
            raise
        finally:
            client.close()

    return JSONResponse(status_code=200, content=_body(request, summary))


@router.post("/{run_id}/notify")
def notify(
    run_id: uuid.UUID,
    request: Request,
    principal: str = Depends(require_internal_bearer),
) -> JSONResponse:
    """Post the triage card, if the run is ready for one."""
    from policy_service.domain import triage
    from policy_service.integrations.slack_client import SlackClient

    key = validate_key(request)
    operation = "runs-notify"
    correlation_id = uuid.UUID(request.state.correlation_id)
    request_hash = canonical_hash(principal, operation, {"run_id": str(run_id)})
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

        slack = SlackClient(bot_token=settings.slack_bot_token)
        try:
            summary = triage.notify(
                conn,
                run_id=run_id,
                correlation_id=correlation_id,
                client=slack,
            )
            idempotency_store.complete(conn, claim.ledger_id, status_code=200, summary=summary)
            conn.commit()
        except triage.NotifyRefusedError as refused:
            # The ordering gate doing its job, but no card was posted, so a
            # person must hear about it and the same key must be able to run
            # again once the run is ready. Recorded as success, the refusal
            # would be replayed for good.
            conn.rollback()
            _release_claim(claim.ledger_id, "NOTIFY_REFUSED")
            return JSONResponse(
                status_code=409,
                content=_body(request, {"error": "NOTIFY_REFUSED", "reason": str(refused)}),
            )
        except Exception:
            conn.rollback()
            _release_claim(claim.ledger_id, "NOTIFY_FAILED")
            raise
        finally:
            slack.close()

    return JSONResponse(status_code=200, content=_body(request, summary))
