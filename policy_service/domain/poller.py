"""The poll and reconcile use cases.

Everything that needs a database or a provider lives here, so the engine in
reconciliation.py stays pure and the API layer stays thin.

The service owns the watermark. n8n sends no cursor and no cutoff, because a
client-supplied cursor is a client-supplied opportunity to skip a bill.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from psycopg import Connection

from policy_service.db.repository import (
    find_duplicate_hits,
    lock_duplicate_keys,
    persist_reconciliation,
)
from policy_service.domain.duplicates import business_key, invoice_number_key
from policy_service.domain.ingestion import (
    bill_hash,
    check_allow_list,
    ingestion_version_key,
)
from policy_service.domain.models import Bill, PurchaseOrder, Tolerances
from policy_service.domain.normalisation import (
    calendar_day,
    split_bill_reference,
    xero_timestamp,
)
from policy_service.domain.reconciliation import reconcile
from policy_service.integrations.xero_parsing import account_reference_hash

CURSOR_NAME = "bills"
TOLERANCE_VERSION = "v1"


@dataclass
class PollReport:
    polled: int = 0
    ingested: int = 0
    skipped_not_allow_listed: int = 0
    skipped_unchanged: int = 0
    run_ids: list[str] = field(default_factory=list)
    cursor_advanced: bool = False


def active_fixtures(conn: Connection) -> dict[str, str]:
    """Xero resource id to fixture id, ACTIVE only (I14)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT xero_resource_id::text, fixture_id::text FROM seed_fixtures "
            "WHERE fixture_status = 'ACTIVE'"
        )
        return dict(cur.fetchall())


@dataclass(frozen=True)
class PageWalk:
    truncated: bool
    last_updated: datetime | None


# Each re-query starts this far before the newest timestamp seen, in case Xero
# compares If-Modified-Since at whole seconds or as strictly-after. Bills read
# twice because of it are recognised and handled once.
REQUERY_OVERLAP = timedelta(seconds=1)


def walk_pages(
    fetch_page: Callable[[datetime | None, int], list[dict]],
    handle: Callable[[dict], None],
    *,
    since: datetime | None,
    page_size: int,
    max_pages: int,
) -> PageWalk:
    """Read every bill modified since `since`, oldest first, until a short page.

    Re-queries from the newest UpdatedDateUTC seen rather than stepping page
    numbers. A bill edited mid-poll moves to the end of the ordering, and with
    page numbers every later bill shifts back one, so the next page started one
    bill too late and skipped it. Each bill version (id and UpdatedDateUTC) is
    handled once, however many queries return it.

    A full page that brings nothing new means more than a page of bills share
    one timestamp; the walk then steps to the next page of that same query so it
    always makes progress. `fetch_page` takes (since, page).

    `truncated` says the request cap stopped the walk with bills possibly left
    unread. `last_updated` is the newest UpdatedDateUTC handled; bills arrive
    oldest first, so everything up to it has been read.
    """
    seen: set[tuple] = set()
    last_updated = None
    page = 1
    for _ in range(max_pages):
        invoices = fetch_page(since, page)
        fresh = 0
        for raw in invoices:
            version = (raw.get("InvoiceID"), raw.get("UpdatedDateUTC"))
            if version in seen:
                continue
            seen.add(version)
            fresh += 1
            handle(raw)
            last_updated = xero_timestamp(raw.get("UpdatedDateUTC")) or last_updated
        if len(invoices) < page_size:
            return PageWalk(truncated=False, last_updated=last_updated)
        newest = xero_timestamp(invoices[-1].get("UpdatedDateUTC"))
        if fresh and newest is not None:
            since, page = newest - REQUERY_OVERLAP, 1
        else:
            page += 1
    return PageWalk(truncated=True, last_updated=last_updated)


def purchase_order_allow_listed(conn: Connection, purchase_order: PurchaseOrder | None) -> bool:
    """I14: the purchase order must itself be on the ACTIVE fixture allow-list.

    The bill was checked at ingestion. Without this, a bill on the list could
    still be reconciled against any purchase order in the organisation.
    """
    if purchase_order is None or purchase_order.purchase_order_id is None:
        return False
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM seed_fixtures WHERE xero_resource_type = 'PURCHASE_ORDER' "
            "AND fixture_status = 'ACTIVE' AND xero_resource_id = %s",
            (purchase_order.purchase_order_id,),
        )
        return cur.fetchone() is not None


# A review case with no posted card for this long is sent through workflow 02
# again. Every notify attempt touches updated_at, so a run whose card keeps
# failing is retried at this interval, not every poll.
UNNOTIFIED_AFTER = timedelta(minutes=15)
MAX_RESENT_PER_POLL = 50


def runs_awaiting_a_card(
    conn: Connection,
    *,
    older_than: timedelta = UNNOTIFIED_AFTER,
    limit: int = MAX_RESENT_PER_POLL,
) -> list[uuid.UUID]:
    """Review cases ready for a card that never got one.

    Workflow 01 sends 02 only the runs it ingested in that poll. A run whose 02
    failed after reconciliation, at semantic review or at notify, was therefore
    never picked up again, including a run already recovered once by the
    stale-attempt cleanup. Re-sent, 02 replays reconcile and semantic review
    and posts the card.

    A card counts only if it was posted for the run's current destination:
    after a hand-off, the previous team's card does not.

    Only runs whose model stage has finished: a stage still PENDING or
    IN_PROGRESS is not ready for a card, and IN_PROGRESS is the stale-attempt
    cleanup's job.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.run_id FROM runs r
             WHERE r.workflow_status = 'REVIEW_READY'
               AND r.semantic_stage_status IN ('NOT_REQUIRED', 'DISABLED',
                     'COMPLETED_WITH_RECOMMENDATION', 'COMPLETED_WITHOUT_RECOMMENDATION')
               AND r.updated_at < now() - %s
               AND NOT EXISTS (SELECT 1 FROM slack_notifications n
                                WHERE n.run_id = r.run_id AND n.post_status = 'POSTED'
                                  AND n.destination = r.triage_destination)
             ORDER BY r.updated_at
             LIMIT %s
            """,
            (older_than, limit),
        )
        return [row[0] for row in cur.fetchall()]


def read_cursor(conn: Connection) -> datetime | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT high_water_mark FROM poll_cursors WHERE cursor_name = %s",
            (CURSOR_NAME,),
        )
        row = cur.fetchone()
    return row[0] if row else None


def advance_cursor(conn: Connection, started_at: datetime, overlap_minutes: int) -> None:
    """Advance ONLY after a successful poll, and deliberately overlap.

    The window re-reads a few minutes every cycle. uq_runs_ingestion_version
    makes that harmless: unchanged content produces the same key and no second
    run. A failed or truncated poll leaves the cursor alone, so it skips
    nothing.
    """
    mark = started_at - timedelta(minutes=overlap_minutes)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO poll_cursors (cursor_name, high_water_mark,
                                      last_poll_started_at, last_poll_ended_at,
                                      last_poll_status)
                 VALUES (%s, %s, %s, now(), 'SUCCEEDED')
            ON CONFLICT (cursor_name) DO UPDATE
                    SET high_water_mark      = EXCLUDED.high_water_mark,
                        last_poll_started_at = EXCLUDED.last_poll_started_at,
                        last_poll_ended_at   = now(),
                        last_poll_status     = 'SUCCEEDED',
                        updated_at           = now()
            """,
            (CURSOR_NAME, mark, started_at),
        )


def ingest_bill(
    conn: Connection,
    bill: Bill,
    fixtures: dict[str, str],
    correlation_id: uuid.UUID,
) -> tuple[uuid.UUID | None, str]:
    """Create a run, or explain why not. Returns (run_id, reason)."""
    decision = check_allow_list(str(bill.invoice_id), fixtures)
    if not decision.allowed:
        return None, decision.reason

    version_key = ingestion_version_key(bill)
    if version_key is None:
        return None, "NO_INGESTION_VERSION"

    run_id = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO runs (run_id, correlation_id, seed_fixture_id,
                              xero_invoice_id, xero_invoice_number,
                              ingestion_version_key, xero_updated_date_utc,
                              bill_hash, workflow_status)
                 VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'INGESTED')
            ON CONFLICT (xero_invoice_id, ingestion_version_key) DO NOTHING
              RETURNING run_id
            """,
            (
                run_id,
                correlation_id,
                decision.fixture_id,
                bill.invoice_id,
                (bill.invoice_number or "").strip(),
                version_key,
                None,
                bill_hash(bill),
            ),
        )
        row = cur.fetchone()
    if row is None:
        return None, "UNCHANGED"
    return row[0], "INGESTED"


def reconcile_run(
    conn: Connection,
    *,
    run_id: uuid.UUID,
    correlation_id: uuid.UUID,
    bill: Bill,
    purchase_order: PurchaseOrder | None,
    chart: frozenset[str],
    po_allow_listed: bool,
    semantic_review_enabled: bool = False,
    tolerances: Tolerances | None = None,
) -> dict:
    """Reconcile and persist, in one transaction.

    The duplicate lookup happens before the engine runs, because the engine is
    pure and takes the collision result as an argument.
    """
    contact_id = str(bill.contact.contact_id) if bill.contact else ""
    invoice_number, po_reference = split_bill_reference(bill.invoice_number)

    invoice_key = None
    if invoice_number:
        # The supplier's number only, never the combined field. Two bills for
        # different orders must not share a duplicate key just because the
        # supplier number was reused.
        invoice_key = invoice_number_key(contact_id, invoice_number)
    date_bucket = calendar_day(bill.date_string, bill.date) or datetime.now(UTC).date().isoformat()
    biz_key = business_key(contact_id, bill.total, bill.currency_code, date_bucket, po_reference)

    lock_duplicate_keys(conn, invoice_key, biz_key)
    invoice_hit, business_hit = find_duplicate_hits(
        conn, run_id=run_id, invoice_key=invoice_key, business_key=biz_key
    )

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE runs SET workflow_status = 'RECONCILING' WHERE run_id = %s",
            (run_id,),
        )

    result = reconcile(
        bill,
        purchase_order,
        chart_of_accounts=chart,
        tolerances=tolerances,
        po_allow_listed=po_allow_listed,
        duplicate_invoice_key=invoice_key,
        duplicate_business_key=biz_key,
        duplicate_invoice_hit=invoice_hit,
        duplicate_business_hit=business_hit,
        semantic_review_enabled=semantic_review_enabled,
    )

    result_id = persist_reconciliation(
        conn,
        run_id=run_id,
        correlation_id=correlation_id,
        result=result,
        tolerance_version=TOLERANCE_VERSION,
        account_reference_version="chart-v1",
        account_reference_hash=account_reference_hash(chart),
        po_number=purchase_order.purchase_order_number if purchase_order else None,
    )

    return {
        "run_id": str(run_id),
        "result_id": str(result_id),
        "outcome": result.outcome.value,
        "exception_codes": [c.value for c in result.exception_codes],
        "triage_destination": (
            result.triage_destination.value if result.triage_destination else None
        ),
        "semantic_permitted": result.semantic_permitted,
        "semantic_gate_reason": result.semantic_gate_reason.value,
        "semantic_stage_status": result.semantic_stage_status.value,
    }
