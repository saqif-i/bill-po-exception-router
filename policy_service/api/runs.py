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

router = APIRouter(prefix="/runs", tags=["runs"])

MAX_BILLS_PER_POLL = 50
POLL_OVERLAP_MINUTES = 5


def _body(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    return {**payload, "correlation_id": getattr(request.state, "correlation_id", None)}


@router.post("/poll")
def poll(request: Request, principal: str = Depends(require_internal_bearer)) -> JSONResponse:
    """Ingest draft ACCPAY bills modified since the watermark.

    The service owns the watermark. n8n sends no cursor and no cutoff, because a
    client-supplied cursor is a client-supplied opportunity to skip a bill.

    The cursor advances only after a poll completes successfully, so a failed or
    truncated poll skips nothing.
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
            payload = client.list_invoices(
                modified_since=mark.isoformat() if mark else None,
                max_records=MAX_BILLS_PER_POLL,
            )
            fixtures = poller.active_fixtures(conn)

            for raw in payload.get("Invoices", []):
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

            poller.advance_cursor(conn, started_at, POLL_OVERLAP_MINUTES)
            report.cursor_advanced = True
            summary = {
                "polled": report.polled,
                "ingested": report.ingested,
                "skipped_not_allow_listed": report.skipped_not_allow_listed,
                "skipped_unchanged": report.skipped_unchanged,
                "run_ids": report.run_ids,
            }
            idempotency_store.complete(conn, claim.ledger_id, status_code=200, summary=summary)
            conn.commit()
        except Exception:
            conn.rollback()
            with get_pool().connection() as other:
                idempotency_store.fail(
                    other, claim.ledger_id, error_class="RETRYABLE", error_code="POLL_FAILED"
                )
                other.commit()
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

        with conn.cursor() as cur:
            cur.execute(
                "SELECT xero_invoice_id, xero_invoice_number FROM runs WHERE run_id = %s",
                (run_id,),
            )
            row = cur.fetchone()
        if row is None:
            raise ServiceError(code="RUN_NOT_FOUND", status_code=404)

        invoice = client.list_invoices(modified_since=None, max_records=MAX_BILLS_PER_POLL)
        match = next(
            (i for i in invoice.get("Invoices", []) if str(i.get("InvoiceID")) == str(row[0])),
            None,
        )
        if match is None:
            raise ServiceError(code="BILL_NO_LONGER_AVAILABLE", status_code=409)

        bill = Bill.model_validate(match)
        from policy_service.domain.normalisation import split_bill_reference

        reference = split_bill_reference(bill.invoice_number)[1]
        purchase_order = None
        if reference:
            try:
                payload = client.get_purchase_order(reference)
                orders = payload.get("PurchaseOrders", [])
                if orders:
                    purchase_order = PurchaseOrder.model_validate(orders[0])
            except Exception:
                purchase_order = None

        summary = poller.reconcile_run(
            conn,
            run_id=run_id,
            correlation_id=correlation_id,
            bill=bill,
            purchase_order=purchase_order,
            chart=client.chart_of_accounts(),
            semantic_review_enabled=get_settings().semantic_review_enabled,
        )
        idempotency_store.complete(conn, claim.ledger_id, status_code=200, summary=summary)
        conn.commit()

    return JSONResponse(status_code=200, content=_body(request, summary))


@router.post("/{run_id}/semantic-review")
def semantic_review(
    run_id: uuid.UUID,
    request: Request,
    principal: str = Depends(require_internal_bearer),
) -> JSONResponse:
    """Part 7 implements this. The route exists now so the n8n workflow can be
    built and exported once rather than re-edited later."""
    validate_key(request)
    return JSONResponse(
        status_code=501,
        content=_body(request, {"error": "NOT_IMPLEMENTED", "implemented_in": "Part 7"}),
    )


@router.post("/{run_id}/notify")
def notify(
    run_id: uuid.UUID,
    request: Request,
    principal: str = Depends(require_internal_bearer),
) -> JSONResponse:
    """Part 8 implements this. Same reason as above."""
    validate_key(request)
    return JSONResponse(
        status_code=501,
        content=_body(request, {"error": "NOT_IMPLEMENTED", "implemented_in": "Part 8"}),
    )
