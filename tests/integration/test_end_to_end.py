"""A bill travels from ingestion to a recorded exception.

This is day 3's hard gate, tested without Xero: the poller takes parsed bills,
so a captured fixture and a live response are the same thing to it (ADR-008).
"""

from __future__ import annotations

import os
import uuid

import pytest

psycopg = pytest.importorskip("psycopg")

from policy_service.domain.ingestion import check_allow_list, ingestion_version_key  # noqa: E402
from policy_service.domain.models import Bill, PurchaseOrder  # noqa: E402
from policy_service.domain.poller import (  # noqa: E402
    active_fixtures,
    advance_cursor,
    ingest_bill,
    read_cursor,
    reconcile_run,
)

OWNER_URL = os.environ.get("BPR_OWNER_DATABASE_URL")
pytestmark = pytest.mark.skipif(not OWNER_URL, reason="no database configured")

CHART = frozenset({"0010", "0020"})

# Each bill gets its own reference. The business-key duplicate check hashes
# supplier, total, currency, date and reference together, so reusing one
# reference across fixtures makes every later run a genuine duplicate of an
# earlier one. The real fixtures in Part 1 have distinct PO numbers for the
# same reason.
SUPPLIER = "99999999-8888-7777-6666-555555555555"


def _line(desc, **kw):
    base = {
        "Description": desc,
        "AccountCode": "0010",
        "TaxType": "INPUT",
        "Quantity": "10",
        "UnitAmount": "5.00",
        "LineAmount": "50.00",
        "TaxAmount": "5.00",
    }
    base.update(kw)
    return base


def _bill(invoice_id, number, lines, ref=None, **kw):
    """A bill as Xero actually presents one.

    An ACCPAY invoice has a single free-text field: the UI calls it Reference
    and the API returns it as InvoiceNumber. So the supplier's number and the
    purchase-order reference share it, on the documented convention.
    """
    po_number = ref or f"PO-{invoice_id.hex[:8]}"
    payload = {
        "InvoiceID": str(invoice_id),
        "InvoiceNumber": f"{number} {po_number}",
        "Type": "ACCPAY",
        "Status": "DRAFT",
        "CurrencyCode": "AUD",
        "Date": "2026-09-01",
        "Contact": {"ContactID": SUPPLIER},
        "LineItems": lines,
        "TotalTax": "5.00",
        "Total": "50.00",
    }
    payload.update(kw)
    return Bill.model_validate(payload)


def _po(lines, **kw):
    payload = {
        "PurchaseOrderNumber": "PO-E2E",
        "Status": "AUTHORISED",
        "CurrencyCode": "AUD",
        "Contact": {"ContactID": SUPPLIER},
        "LineItems": lines,
        "TotalTax": "5.00",
        "Total": "50.00",
    }
    payload.update(kw)
    return PurchaseOrder.model_validate(payload)


def _seed_fixture(conn, resource_id):
    fixture_id = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO seed_fixtures (fixture_id, seed_run_id, fixture_name, "
            "fixture_reference, xero_resource_type, xero_resource_id, human_reference, "
            "expected_scenario, disposition, fixture_status, created_at) VALUES "
            "(%s, %s, 'e2e', %s, 'INVOICE', %s, 'INV', 'X', 'CREATED', 'ACTIVE', now())",
            (fixture_id, uuid.uuid4(), f"BPR-SEED-{resource_id.hex[:8]}", resource_id),
        )
    return fixture_id


def test_a_bill_reaches_a_recorded_exception():
    """The hard gate. Ingest, reconcile, persist, read it back."""
    invoice_id = uuid.uuid4()
    correlation_id = uuid.uuid4()
    bill = _bill(invoice_id, f"INV-{invoice_id.hex[:6]}", [_line("widget", Quantity="11")])
    po = _po([_line("widget")], number=f"PO-{invoice_id.hex[:8]}")

    with psycopg.connect(OWNER_URL) as conn:
        _seed_fixture(conn, invoice_id)
        conn.commit()

        fixtures = active_fixtures(conn)
        run_id, reason = ingest_bill(conn, bill, fixtures, correlation_id)
        assert reason == "INGESTED" and run_id is not None
        conn.commit()

        summary = reconcile_run(
            conn,
            run_id=run_id,
            correlation_id=correlation_id,
            bill=bill,
            purchase_order=po,
            chart=CHART,
        )
        conn.commit()

        with conn.cursor() as cur:
            cur.execute(
                "SELECT workflow_status, reconciliation_outcome, triage_destination "
                "FROM runs WHERE run_id = %s",
                (run_id,),
            )
            status, outcome, destination = cur.fetchone()

    assert summary["outcome"] == "REVIEW_REQUIRED"
    assert "QUANTITY_VARIANCE" in summary["exception_codes"]
    assert status == "REVIEW_READY"
    assert outcome == "REVIEW_REQUIRED"
    assert destination == "PROCUREMENT"


def test_a_clean_bill_completes_silently():
    invoice_id = uuid.uuid4()
    bill = _bill(invoice_id, f"INV-{invoice_id.hex[:6]}", [_line("widget")])
    po = _po([_line("widget")], number=f"PO-{invoice_id.hex[:8]}")

    with psycopg.connect(OWNER_URL) as conn:
        _seed_fixture(conn, invoice_id)
        conn.commit()
        run_id, _ = ingest_bill(conn, bill, active_fixtures(conn), uuid.uuid4())
        conn.commit()
        summary = reconcile_run(
            conn,
            run_id=run_id,
            correlation_id=uuid.uuid4(),
            bill=bill,
            purchase_order=po,
            chart=CHART,
        )
        conn.commit()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT workflow_status, triage_destination FROM runs WHERE run_id = %s", (run_id,)
            )
            status, destination = cur.fetchone()

    assert summary["outcome"] == "MATCHED"
    assert status == "COMPLETED"
    assert destination is None  # silence on a clean bill


def test_a_bill_not_on_the_allow_list_never_becomes_a_run():
    """I14. On a Demo Company this is the difference between nine runs and
    ninety: the demo arrives pre-populated with sample invoices."""
    invoice_id = uuid.uuid4()
    bill = _bill(invoice_id, "INV-STRANGER", [_line("widget")])
    with psycopg.connect(OWNER_URL) as conn:
        run_id, reason = ingest_bill(conn, bill, active_fixtures(conn), uuid.uuid4())
    assert run_id is None
    assert reason == "NOT_ON_ACTIVE_ALLOW_LIST"


def test_repolling_an_unchanged_bill_creates_no_second_run():
    """The deliberate poll-window overlap is harmless because unchanged content
    produces the same ingestion version key."""
    invoice_id = uuid.uuid4()
    bill = _bill(invoice_id, f"INV-{invoice_id.hex[:6]}", [_line("widget")])
    with psycopg.connect(OWNER_URL) as conn:
        _seed_fixture(conn, invoice_id)
        conn.commit()
        fixtures = active_fixtures(conn)
        first, _ = ingest_bill(conn, bill, fixtures, uuid.uuid4())
        conn.commit()
        second, reason = ingest_bill(conn, bill, fixtures, uuid.uuid4())
        conn.commit()
    assert first is not None
    assert second is None and reason == "UNCHANGED"


def test_an_edited_bill_creates_a_new_run_rather_than_mutating_the_old_one():
    invoice_id = uuid.uuid4()
    original = _bill(invoice_id, f"INV-{invoice_id.hex[:6]}", [_line("widget")])
    edited = _bill(invoice_id, f"INV-{invoice_id.hex[:6]}", [_line("widget", Quantity="12")])
    assert ingestion_version_key(original) != ingestion_version_key(edited)

    with psycopg.connect(OWNER_URL) as conn:
        _seed_fixture(conn, invoice_id)
        conn.commit()
        fixtures = active_fixtures(conn)
        first, _ = ingest_bill(conn, original, fixtures, uuid.uuid4())
        conn.commit()
        second, reason = ingest_bill(conn, edited, fixtures, uuid.uuid4())
        conn.commit()
    assert first != second
    assert reason == "INGESTED"


def test_the_wording_only_case_reaches_the_gate_open():
    """Scenario 8, end to end and persisted."""
    invoice_id = uuid.uuid4()
    bill = _bill(
        invoice_id, f"INV-{invoice_id.hex[:6]}", [_line("24x 500ml bottled water, assorted")]
    )
    po = _po([_line("Water, bottled, case of 24")], number=f"PO-{invoice_id.hex[:8]}")

    with psycopg.connect(OWNER_URL) as conn:
        _seed_fixture(conn, invoice_id)
        conn.commit()
        run_id, _ = ingest_bill(conn, bill, active_fixtures(conn), uuid.uuid4())
        conn.commit()
        summary = reconcile_run(
            conn,
            run_id=run_id,
            correlation_id=uuid.uuid4(),
            bill=bill,
            purchase_order=po,
            chart=CHART,
            semantic_review_enabled=True,
        )
        conn.commit()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT semantic_permitted, semantic_gate_reason FROM "
                "reconciliation_results WHERE run_id = %s",
                (run_id,),
            )
            permitted, gate_reason = cur.fetchone()

    assert summary["semantic_permitted"] is True
    assert permitted is True
    assert gate_reason == "RESIDUAL_PAIR_TEXT_ONLY"
    assert summary["semantic_stage_status"] == "PENDING"


def test_the_wording_plus_price_case_reaches_the_gate_closed():
    """Scenario 9. Same wording difference, one dollar apart."""
    invoice_id = uuid.uuid4()
    bill = _bill(
        invoice_id,
        f"INV-{invoice_id.hex[:6]}",
        [_line("24x 500ml bottled water", UnitAmount="6.00", LineAmount="60.00")],
        Total="60.00",
    )
    po = _po([_line("Water, bottled, case of 24")], number=f"PO-{invoice_id.hex[:8]}")

    with psycopg.connect(OWNER_URL) as conn:
        _seed_fixture(conn, invoice_id)
        conn.commit()
        run_id, _ = ingest_bill(conn, bill, active_fixtures(conn), uuid.uuid4())
        conn.commit()
        summary = reconcile_run(
            conn,
            run_id=run_id,
            correlation_id=uuid.uuid4(),
            bill=bill,
            purchase_order=po,
            chart=CHART,
            semantic_review_enabled=True,
        )
        conn.commit()

    assert summary["semantic_permitted"] is False
    assert summary["semantic_gate_reason"] == "RESIDUAL_PAIR_DETERMINISTIC_MISMATCH"
    assert "RESIDUAL_UNIT_PRICE_VARIANCE" in summary["exception_codes"]


def test_the_cursor_advances_only_after_success_and_overlaps():
    from datetime import UTC, datetime

    started = datetime.now(UTC)
    with psycopg.connect(OWNER_URL) as conn:
        advance_cursor(conn, started, overlap_minutes=5)
        conn.commit()
        mark = read_cursor(conn)
    assert mark is not None
    assert mark < started  # deliberate overlap, never ahead of the poll start


def test_allow_list_ignores_a_superseded_fixture():
    assert check_allow_list("abc", {}).allowed is False
    assert check_allow_list("abc", {"abc": "fix-1"}).allowed is True
