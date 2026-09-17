"""Notification and the human decision, against a real database.

A fake Slack client stands in for the API, so every branch runs without a
workspace, a tunnel or a token.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field

import pytest

psycopg = pytest.importorskip("psycopg")

from policy_service.db.repository import persist_reconciliation  # noqa: E402
from policy_service.domain import triage  # noqa: E402
from policy_service.domain.enums import TriageAction  # noqa: E402
from policy_service.domain.models import Bill, PurchaseOrder  # noqa: E402
from policy_service.domain.reconciliation import reconcile  # noqa: E402
from policy_service.integrations.slack_client import PostOutcome  # noqa: E402

OWNER_URL = os.environ.get("BPR_OWNER_DATABASE_URL")
pytestmark = pytest.mark.skipif(not OWNER_URL, reason="no database configured")

CHART = frozenset({"0010"})
SUPPLIER = "44444444-4444-4444-4444-444444444444"


@dataclass
class FakeSlack:
    post_result: PostOutcome = field(
        default_factory=lambda: PostOutcome(True, message_ts="1700000000.000100")
    )
    update_result: PostOutcome = field(default_factory=lambda: PostOutcome(True))
    posts: list = field(default_factory=list)
    updates: list = field(default_factory=list)

    def post_card(self, **kwargs) -> PostOutcome:
        self.posts.append(kwargs)
        return self.post_result

    def update_card(self, **kwargs) -> PostOutcome:
        self.updates.append(kwargs)
        return self.update_result


def _line(desc, **over):
    base = {
        "Description": desc,
        "AccountCode": "0010",
        "TaxType": "INPUT",
        "Quantity": "10",
        "UnitAmount": "120.00",
        "LineAmount": "1200.00",
        "TaxAmount": "120.00",
    }
    base.update(over)
    return base


def _seed(conn, bill_over=None):
    """A run at REVIEW_READY with a quantity variance, as Part 6 leaves it."""
    invoice_id, fixture_id, run_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    number = f"INV-{invoice_id.hex[:6]}"
    po_number = f"PO-{invoice_id.hex[:8]}"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO seed_fixtures (fixture_id, seed_run_id, fixture_name, "
            "fixture_reference, xero_resource_type, xero_resource_id, human_reference, "
            "expected_scenario, disposition, fixture_status, created_at) VALUES "
            "(%s, %s, 't', %s, 'INVOICE', %s, %s, 'X', 'CREATED', 'ACTIVE', now())",
            (fixture_id, uuid.uuid4(), f"BPR-SEED-{invoice_id.hex[:8]}", invoice_id, number),
        )
        cur.execute(
            "INSERT INTO runs (run_id, correlation_id, seed_fixture_id, "
            "xero_invoice_id, xero_invoice_number, ingestion_version_key, bill_hash, "
            "workflow_status) VALUES (%s, %s, %s, %s, %s, %s, 'h', 'RECONCILING')",
            (run_id, uuid.uuid4(), fixture_id, invoice_id, number, f"v-{run_id.hex[:8]}"),
        )

    bill = Bill.model_validate(
        {
            "InvoiceID": str(invoice_id),
            "InvoiceNumber": f"{number} {po_number}",
            "Type": "ACCPAY",
            "Status": "DRAFT",
            "CurrencyCode": "AUD",
            "Date": "2026-09-01",
            "Contact": {"ContactID": SUPPLIER},
            "LineItems": [
                _line("Office chairs, ergonomic mesh", **(bill_over or {"Quantity": "11"}))
            ],
            "TotalTax": "120.00",
            "Total": "1200.00",
        }
    )
    order = PurchaseOrder.model_validate(
        {
            "PurchaseOrderNumber": po_number,
            "Status": "AUTHORISED",
            "CurrencyCode": "AUD",
            "Contact": {"ContactID": SUPPLIER},
            "LineItems": [_line("Office chairs, ergonomic mesh")],
            "TotalTax": "120.00",
            "Total": "1200.00",
        }
    )

    result = reconcile(bill, order, chart_of_accounts=CHART)
    persist_reconciliation(
        conn,
        run_id=run_id,
        correlation_id=uuid.uuid4(),
        result=result,
        tolerance_version="v1",
        account_reference_version="chart-v1",
        account_reference_hash="d" * 64,
    )
    return run_id


def test_a_card_is_posted_to_the_routed_channel():
    with psycopg.connect(OWNER_URL) as conn:
        run_id = _seed(conn)
        conn.commit()
        client = FakeSlack()
        summary = triage.notify(conn, run_id=run_id, correlation_id=uuid.uuid4(), client=client)
        conn.commit()
        with conn.cursor() as cur:
            cur.execute("SELECT workflow_status FROM runs WHERE run_id=%s", (run_id,))
            status = cur.fetchone()[0]

    assert summary["posted"] is True
    assert summary["channel"] == "ap-procurement"  # quantity variance
    assert status == "AWAITING_TRIAGE"
    assert client.posts[0]["text"].startswith("Bill INV-")


def test_notifying_twice_posts_one_card():
    with psycopg.connect(OWNER_URL) as conn:
        run_id = _seed(conn)
        conn.commit()
        client = FakeSlack()
        triage.notify(conn, run_id=run_id, correlation_id=uuid.uuid4(), client=client)
        conn.commit()
        again = triage.notify(conn, run_id=run_id, correlation_id=uuid.uuid4(), client=client)
        conn.commit()
    assert again["already"] is True
    assert len(client.posts) == 1


def test_an_unknown_dispatch_is_recorded_as_a_possible_duplicate():
    """A timeout may or may not have delivered. Calling that a failure would be
    a claim we cannot support."""
    with psycopg.connect(OWNER_URL) as conn:
        run_id = _seed(conn)
        conn.commit()
        client = FakeSlack(post_result=PostOutcome(False, error="TIMEOUT", dispatch_unknown=True))
        summary = triage.notify(conn, run_id=run_id, correlation_id=uuid.uuid4(), client=client)
        conn.commit()
        with conn.cursor() as cur:
            cur.execute("SELECT post_status FROM slack_notifications WHERE run_id=%s", (run_id,))
            assert cur.fetchone()[0] == "POSSIBLE_DUPLICATE"
    assert summary["posted"] is False


def test_notification_is_refused_while_a_semantic_attempt_is_running():
    """I31, the ordering gate. A card posted now would change under the
    reviewer when the model stage resolves."""
    from policy_service.domain import semantic

    with psycopg.connect(OWNER_URL) as conn:
        run_id = _seed(conn, bill_over={})  # clean numbers, wording only
        conn.commit()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE runs SET semantic_stage_status='PENDING' WHERE run_id=%s", (run_id,)
            )
        conn.commit()
        semantic.start_attempt(
            conn, run_id=run_id, correlation_id=uuid.uuid4(), model_id="m", enabled_flag=True
        )
        conn.commit()
        with pytest.raises(triage.NotifyRefusedError, match="SEMANTIC_ATTEMPT_STILL_STARTED"):
            triage.notify(conn, run_id=run_id, correlation_id=uuid.uuid4(), client=FakeSlack())


def test_a_decision_records_what_the_person_was_shown():
    """Six weeks later, 'why was this allowed through' is answerable."""
    with psycopg.connect(OWNER_URL) as conn:
        run_id = _seed(conn)
        conn.commit()
        triage.notify(conn, run_id=run_id, correlation_id=uuid.uuid4(), client=FakeSlack())
        conn.commit()
        result = triage.record_decision(
            conn,
            run_id=run_id,
            action=TriageAction.SEND_TO_PROCUREMENT,
            user_id="U123",
            user_name="saqif",
            interaction_id=f"i-{uuid.uuid4()}",
            message_ts="1700000000.000100",
        )
        conn.commit()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT action, decided_by, shown_exception_codes, "
                "shown_destination, shown_gate_reason FROM triage_decisions "
                "WHERE run_id=%s",
                (run_id,),
            )
            action, by, codes, destination, gate = cur.fetchone()
            cur.execute("SELECT workflow_status FROM runs WHERE run_id=%s", (run_id,))
            status = cur.fetchone()[0]

    assert result["recorded"] is True
    assert action == "SEND_TO_PROCUREMENT"
    assert by == "U123"
    assert "QUANTITY_VARIANCE" in codes
    assert destination == "PROCUREMENT"
    assert gate  # recorded on every decision, even when no model ran
    assert status == "COMPLETED"


def test_a_retried_interaction_does_not_produce_a_second_decision():
    """Slack retries a delivery it believes failed."""
    interaction = f"i-{uuid.uuid4()}"
    with psycopg.connect(OWNER_URL) as conn:
        run_id = _seed(conn)
        conn.commit()
        triage.notify(conn, run_id=run_id, correlation_id=uuid.uuid4(), client=FakeSlack())
        conn.commit()
        first = triage.record_decision(
            conn,
            run_id=run_id,
            action=TriageAction.MARK_REVIEWED,
            user_id="U1",
            user_name=None,
            interaction_id=interaction,
            message_ts=None,
        )
        conn.commit()
        second = triage.record_decision(
            conn,
            run_id=run_id,
            action=TriageAction.MARK_REVIEWED,
            user_id="U1",
            user_name=None,
            interaction_id=interaction,
            message_ts=None,
        )
        conn.commit()
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM triage_decisions WHERE run_id=%s", (run_id,))
            assert cur.fetchone()[0] == 1
    assert first["recorded"] is True
    assert second["recorded"] is False


def test_a_decision_is_immutable():
    with psycopg.connect(OWNER_URL) as conn:
        run_id = _seed(conn)
        conn.commit()
        triage.notify(conn, run_id=run_id, correlation_id=uuid.uuid4(), client=FakeSlack())
        conn.commit()
        triage.record_decision(
            conn,
            run_id=run_id,
            action=TriageAction.ESCALATE,
            user_id="U1",
            user_name=None,
            interaction_id=f"i-{uuid.uuid4()}",
            message_ts=None,
        )
        conn.commit()

    with (
        psycopg.connect(OWNER_URL) as conn,
        pytest.raises(psycopg.errors.DatabaseError),
        conn.cursor() as cur,
    ):
        cur.execute("UPDATE triage_decisions SET action='MARK_REVIEWED' WHERE run_id=%s", (run_id,))


def test_the_card_update_is_best_effort_and_the_decision_survives_its_failure():
    """ADR-007. The database is authoritative; the card is a view."""
    with psycopg.connect(OWNER_URL) as conn:
        run_id = _seed(conn)
        conn.commit()
        client = FakeSlack(update_result=PostOutcome(False, error="TIMEOUT", dispatch_unknown=True))
        triage.notify(conn, run_id=run_id, correlation_id=uuid.uuid4(), client=client)
        conn.commit()
        result = triage.record_decision(
            conn,
            run_id=run_id,
            action=TriageAction.MARK_REVIEWED,
            user_id="U1",
            user_name=None,
            interaction_id=f"i-{uuid.uuid4()}",
            message_ts="1700000000.000100",
        )
        conn.commit()
        updated = triage.update_card_best_effort(
            conn,
            run_id=run_id,
            context=result["context"],
            action=TriageAction.MARK_REVIEWED,
            user_id="U1",
            decided_at="01 Sep 2026, 10:00 UTC",
            client=client,
        )
        conn.commit()
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM triage_decisions WHERE run_id=%s", (run_id,))
            decisions = cur.fetchone()[0]

    assert updated is False  # the card is stale
    assert decisions == 1  # the decision is not
