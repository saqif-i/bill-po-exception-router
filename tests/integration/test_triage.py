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
    """A run at REVIEW_READY with a quantity variance, as reconciliation leaves it."""
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

    result = reconcile(bill, order, chart_of_accounts=CHART, po_allow_listed=True)
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
            action=TriageAction.ESCALATE,
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
    assert action == "ESCALATE"  # a control the procurement card offers
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


def _status(conn, run_id):
    with conn.cursor() as cur:
        cur.execute("SELECT workflow_status FROM runs WHERE run_id=%s", (run_id,))
        return cur.fetchone()[0]


@dataclass
class ObservingSlack(FakeSlack):
    """Looks at the database from the outside while the card is being posted."""

    notify_conn: object = None
    run_id: object = None
    seen: dict = field(default_factory=dict)

    def post_card(self, **kwargs) -> PostOutcome:
        from psycopg import errors
        from psycopg.pq import TransactionStatus

        self.seen["transaction"] = self.notify_conn.info.transaction_status
        self.seen["idle"] = TransactionStatus.IDLE
        with psycopg.connect(OWNER_URL) as other:
            self.seen["status"] = _status(other, self.run_id)
            try:
                with other.cursor() as cur:
                    cur.execute(
                        "SELECT 1 FROM runs WHERE run_id=%s FOR UPDATE NOWAIT", (self.run_id,)
                    )
                self.seen["locked"] = False
            except errors.LockNotAvailable:
                self.seen["locked"] = True
            other.rollback()
            try:
                # A separate fake: re-entering this one would recurse if a
                # regression let the second notify through.
                triage.notify(
                    other, run_id=self.run_id, correlation_id=uuid.uuid4(), client=FakeSlack()
                )
                self.seen["second_notify"] = "ran"
            except triage.NotifyRefusedError as refused:
                self.seen["second_notify"] = str(refused)
        return super().post_card(**kwargs)


def test_slack_is_called_with_no_transaction_or_lock_held():
    """I12: nothing is held open across the call. I31: the run is claimed as
    NOTIFY_PENDING first, so a second notify is refused rather than posting a
    second card."""
    with psycopg.connect(OWNER_URL) as conn:
        run_id = _seed(conn)
        conn.commit()
        client = ObservingSlack(notify_conn=conn, run_id=run_id)
        summary = triage.notify(conn, run_id=run_id, correlation_id=uuid.uuid4(), client=client)
        conn.commit()
        final = _status(conn, run_id)

    assert client.seen["transaction"] == client.seen["idle"]
    assert client.seen["locked"] is False
    assert client.seen["status"] == "NOTIFY_PENDING"
    assert client.seen["second_notify"] == "WORKFLOW_STATUS_NOTIFY_PENDING"
    assert len(client.posts) == 1
    assert summary["posted"] is True
    assert final == "AWAITING_TRIAGE"


def test_a_failed_post_returns_the_run_for_a_retry():
    with psycopg.connect(OWNER_URL) as conn:
        run_id = _seed(conn)
        conn.commit()
        failing = FakeSlack(post_result=PostOutcome(False, error="channel_not_found"))
        triage.notify(conn, run_id=run_id, correlation_id=uuid.uuid4(), client=failing)
        conn.commit()
        after_failure = _status(conn, run_id)
        retry = triage.notify(conn, run_id=run_id, correlation_id=uuid.uuid4(), client=FakeSlack())
        conn.commit()
        after_retry = _status(conn, run_id)

    assert after_failure == "REVIEW_READY"
    assert retry["posted"] is True
    assert after_retry == "AWAITING_TRIAGE"


class ExplodingSlack(FakeSlack):
    def post_card(self, **kwargs) -> PostOutcome:
        raise ValueError("Expecting value: line 1 column 1 (char 0)")


def test_an_exception_from_slack_is_recorded_rather_than_stranding_the_run():
    """The request may have gone out, so the outcome is unknown. Raising would
    leave the run in NOTIFY_PENDING with nothing recorded."""
    with psycopg.connect(OWNER_URL) as conn:
        run_id = _seed(conn)
        conn.commit()
        summary = triage.notify(
            conn, run_id=run_id, correlation_id=uuid.uuid4(), client=ExplodingSlack()
        )
        conn.commit()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT post_status, post_error FROM slack_notifications WHERE run_id=%s",
                (run_id,),
            )
            post_status, post_error = cur.fetchone()
        final = _status(conn, run_id)

    assert summary["posted"] is False
    assert post_status == "POSSIBLE_DUPLICATE"
    assert post_error == "ValueError"
    assert final == "REVIEW_READY"


def test_a_click_that_arrives_before_the_post_is_recorded_is_accepted():
    """The card exists if someone clicked it, even if notify has not yet
    recorded the post."""
    with psycopg.connect(OWNER_URL) as conn:
        run_id = _seed(conn)
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE runs SET workflow_status='NOTIFY_PENDING' WHERE run_id=%s", (run_id,)
            )
        conn.commit()
        result = triage.record_decision(
            conn,
            run_id=run_id,
            action=TriageAction.MARK_REVIEWED,
            user_id="U123",
            user_name="saqif",
            interaction_id=f"i-{uuid.uuid4()}",
            message_ts="1700000000.000100",
        )
        conn.commit()
        final = _status(conn, run_id)

    assert result["recorded"] is True
    assert final == "COMPLETED"


class WatchingUpdate(FakeSlack):
    """Records whether a transaction is open while the card is updated."""

    def __init__(self, conn) -> None:
        super().__init__()
        self.conn = conn
        self.transaction_during_update = None

    def update_card(self, **kwargs) -> PostOutcome:
        self.transaction_during_update = self.conn.info.transaction_status
        return super().update_card(**kwargs)


def test_the_card_update_is_made_with_no_transaction_open():
    """I12: the read that finds the card is committed before Slack is called."""
    from psycopg.pq import TransactionStatus

    with psycopg.connect(OWNER_URL) as conn:
        run_id = _seed(conn)
        conn.commit()
        client = WatchingUpdate(conn)
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

    assert updated is True
    assert client.transaction_during_update == TransactionStatus.IDLE


def _backdate(conn, run_id, minutes):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE runs SET updated_at = now() - make_interval(mins => %s) WHERE run_id = %s",
            (minutes, run_id),
        )
    conn.commit()


def test_a_review_case_with_no_card_is_found_for_resending():
    """A run whose workflow 02 failed after reconciliation was never picked up
    again, including one already recovered by the stale-attempt cleanup."""
    from policy_service.domain.poller import runs_awaiting_a_card

    with psycopg.connect(OWNER_URL) as conn:
        waiting = _seed(conn)
        conn.commit()
        _backdate(conn, waiting, 16)

        recent = _seed(conn)
        conn.commit()

        posted = _seed(conn)
        conn.commit()
        triage.notify(conn, run_id=posted, correlation_id=uuid.uuid4(), client=FakeSlack())
        conn.commit()

        found = runs_awaiting_a_card(conn, limit=100_000)

    assert waiting in found
    assert recent not in found
    assert posted not in found


# --- hand-off: "Send to finance" moves the case, it does not close it ---------
AP_TS = "1700000000.000100"
FINANCE_TS = "1700000001.000200"


def _seed_ap_review(conn):
    """Every number agrees and only the wording differs: routed to AP review."""
    run_id = _seed(conn, bill_over={"Description": "Ergonomic mesh task chairs"})
    conn.commit()
    return run_id


def _decide(conn, run_id, action, message_ts, user="U123"):
    result = triage.record_decision(
        conn,
        run_id=run_id,
        action=action,
        user_id=user,
        user_name=None,
        interaction_id=f"i-{uuid.uuid4()}",
        message_ts=message_ts,
    )
    conn.commit()
    return result


def _run(conn, run_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT workflow_status, triage_destination FROM runs WHERE run_id = %s", (run_id,)
        )
        return cur.fetchone()


def test_send_to_finance_hands_the_case_to_finance_whose_decision_closes_it():
    with psycopg.connect(OWNER_URL) as conn:
        run_id = _seed_ap_review(conn)
        ap = FakeSlack(post_result=PostOutcome(True, message_ts=AP_TS, channel_id="C-AP"))
        triage.notify(conn, run_id=run_id, correlation_id=uuid.uuid4(), client=ap)
        conn.commit()

        handed = _decide(conn, run_id, TriageAction.SEND_TO_FINANCE, AP_TS)
        after_handoff = _run(conn, run_id)

        finance = FakeSlack(
            post_result=PostOutcome(True, message_ts=FINANCE_TS, channel_id="C-FIN")
        )
        posted = triage.notify(conn, run_id=run_id, correlation_id=uuid.uuid4(), client=finance)
        conn.commit()
        after_card = _run(conn, run_id)

        final = _decide(conn, run_id, TriageAction.MARK_REVIEWED, FINANCE_TS, user="U999")
        after_final = _run(conn, run_id)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT action, decided_by, shown_destination, is_final FROM triage_decisions "
                "WHERE run_id = %s ORDER BY decided_at",
                (run_id,),
            )
            decisions = cur.fetchall()

    assert ap.posts[0]["channel"] == "ap-review"
    assert handed["recorded"] is True and handed["handoff_to"].value == "FINANCE"
    assert after_handoff == ("REVIEW_READY", "FINANCE")
    assert posted["posted"] is True and posted["channel"] == "ap-finance"
    card = str(finance.posts[0]["blocks"])
    assert "Sent here from ap review by <@U123>" in card
    assert "SEND_TO_PROCUREMENT" not in card  # finance's own controls
    assert after_card == ("AWAITING_TRIAGE", "FINANCE")
    assert final["recorded"] is True and final["handoff_to"] is None
    assert after_final == ("COMPLETED", "FINANCE")
    assert decisions == [
        ("SEND_TO_FINANCE", "U123", "AP_REVIEW", False),
        ("MARK_REVIEWED", "U999", "FINANCE", True),
    ]


def test_a_click_on_the_card_a_hand_off_replaced_is_refused():
    """The AP card is no longer where the case is."""
    with psycopg.connect(OWNER_URL) as conn:
        run_id = _seed_ap_review(conn)
        triage.notify(
            conn,
            run_id=run_id,
            correlation_id=uuid.uuid4(),
            client=FakeSlack(post_result=PostOutcome(True, message_ts=AP_TS)),
        )
        conn.commit()
        _decide(conn, run_id, TriageAction.SEND_TO_FINANCE, AP_TS)
        triage.notify(
            conn,
            run_id=run_id,
            correlation_id=uuid.uuid4(),
            client=FakeSlack(post_result=PostOutcome(True, message_ts=FINANCE_TS)),
        )
        conn.commit()
        with pytest.raises(triage.DecisionRefusedError, match="CARD_SUPERSEDED"):
            _decide(conn, run_id, TriageAction.MARK_REVIEWED, AP_TS)


def test_a_control_the_current_team_is_not_offered_is_refused():
    with psycopg.connect(OWNER_URL) as conn:
        run_id = _seed_ap_review(conn)
        triage.notify(
            conn,
            run_id=run_id,
            correlation_id=uuid.uuid4(),
            client=FakeSlack(post_result=PostOutcome(True, message_ts=AP_TS)),
        )
        conn.commit()
        _decide(conn, run_id, TriageAction.SEND_TO_FINANCE, AP_TS)
        triage.notify(
            conn,
            run_id=run_id,
            correlation_id=uuid.uuid4(),
            client=FakeSlack(post_result=PostOutcome(True, message_ts=FINANCE_TS)),
        )
        conn.commit()
        with pytest.raises(triage.DecisionRefusedError, match="ACTION_NOT_OFFERED_FOR_FINANCE"):
            _decide(conn, run_id, TriageAction.SEND_TO_PROCUREMENT, FINANCE_TS)


def test_the_schema_allows_one_final_decision_and_never_a_final_hand_off():
    from psycopg import errors

    with psycopg.connect(OWNER_URL) as conn:
        run_id = _seed_ap_review(conn)

        def insert(action, is_final):
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO triage_decisions (decision_id, run_id, correlation_id, action, "
                    "decided_by, shown_exception_codes, shown_destination, shown_gate_reason, "
                    "slack_interaction_id, is_final) VALUES (%s, %s, %s, %s, 'U1', '{}', "
                    "'AP_REVIEW', 'X', %s, %s)",
                    (uuid.uuid4(), run_id, uuid.uuid4(), action, f"i-{uuid.uuid4()}", is_final),
                )

        insert("SEND_TO_FINANCE", False)
        insert("MARK_REVIEWED", True)
        conn.commit()
        with pytest.raises(errors.UniqueViolation):
            insert("ESCALATE", True)
        conn.rollback()
        with pytest.raises(errors.CheckViolation):
            insert("SEND_TO_PROCUREMENT", True)
        conn.rollback()


def test_after_a_hand_off_the_decision_updates_the_new_card_in_its_own_channel():
    """The card record kept the AP channel beside the finance card's timestamp,
    so the update after finance's decision edited nothing and left the finance
    card's buttons live."""
    with psycopg.connect(OWNER_URL) as conn:
        run_id = _seed_ap_review(conn)
        triage.notify(
            conn,
            run_id=run_id,
            correlation_id=uuid.uuid4(),
            client=FakeSlack(post_result=PostOutcome(True, message_ts=AP_TS, channel_id="C-AP")),
        )
        conn.commit()
        _decide(conn, run_id, TriageAction.SEND_TO_FINANCE, AP_TS)
        triage.notify(
            conn,
            run_id=run_id,
            correlation_id=uuid.uuid4(),
            client=FakeSlack(
                post_result=PostOutcome(True, message_ts=FINANCE_TS, channel_id="C-FIN")
            ),
        )
        conn.commit()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT channel, destination, message_ts, card_updated_at "
                "FROM slack_notifications WHERE run_id = %s",
                (run_id,),
            )
            record = cur.fetchone()

        final = _decide(conn, run_id, TriageAction.MARK_REVIEWED, FINANCE_TS)
        slack = FakeSlack()
        updated = triage.update_card_best_effort(
            conn,
            run_id=run_id,
            context=final["context"],
            action=TriageAction.MARK_REVIEWED,
            user_id="U123",
            decided_at="now",
            client=slack,
        )
        conn.commit()

    assert record == ("C-FIN", "FINANCE", FINANCE_TS, None)
    assert updated is True
    assert (slack.updates[0]["channel"], slack.updates[0]["message_ts"]) == ("C-FIN", FINANCE_TS)
