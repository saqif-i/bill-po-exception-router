"""The semantic stage lifecycle, against a real database and a fake provider.

No API key, no network. The fake returns whatever the test needs, so every
branch of the lifecycle is exercised deterministically.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass

import pytest

psycopg = pytest.importorskip("psycopg")

from policy_service.db.repository import persist_reconciliation  # noqa: E402
from policy_service.domain import semantic  # noqa: E402
from policy_service.domain.models import Bill, PurchaseOrder  # noqa: E402
from policy_service.domain.reconciliation import reconcile  # noqa: E402
from policy_service.integrations.claude_client import ProviderResult  # noqa: E402
from policy_service.integrations.claude_contract import (  # noqa: E402
    Rejection,
    RejectionReason,
)

OWNER_URL = os.environ.get("BPR_OWNER_DATABASE_URL")
pytestmark = pytest.mark.skipif(not OWNER_URL, reason="no database configured")

CHART = frozenset({"0010"})
SUPPLIER = "33333333-3333-3333-3333-333333333333"
PO_TEXT = "Water, bottled, case of 24"
BILL_TEXT = "24x 500ml bottled water, assorted"


@dataclass
class FakeClient:
    """Stands in for the provider. `model` is read by start_attempt."""

    result: ProviderResult
    model: str = "fake-model-1"

    def review(self, **_kwargs) -> ProviderResult:
        return self.result


def _line(desc, **over):
    base = {
        "Description": desc,
        "AccountCode": "0010",
        "TaxType": "INPUT",
        "Quantity": "5",
        "UnitAmount": "48.00",
        "LineAmount": "240.00",
        "TaxAmount": "24.00",
    }
    base.update(over)
    return base


def _seed_wording_run(conn, bill_over=None):
    """A run sitting at REVIEW_READY with the gate open, as Part 6 leaves it."""
    invoice_id, fixture_id, run_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    number = f"INV-{invoice_id.hex[:6]}"
    po_number = f"PO-{invoice_id.hex[:8]}"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO seed_fixtures (fixture_id, seed_run_id, fixture_name, "
            "fixture_reference, xero_resource_type, xero_resource_id, human_reference, "
            "expected_scenario, disposition, fixture_status, created_at) VALUES "
            "(%s, %s, 'sem', %s, 'INVOICE', %s, %s, 'X', 'CREATED', 'ACTIVE', now())",
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
            "LineItems": [_line(BILL_TEXT, **(bill_over or {}))],
            "TotalTax": "24.00",
            "Total": "240.00",
        }
    )
    order = PurchaseOrder.model_validate(
        {
            "PurchaseOrderNumber": po_number,
            "Status": "AUTHORISED",
            "CurrencyCode": "AUD",
            "Contact": {"ContactID": SUPPLIER},
            "LineItems": [_line(PO_TEXT)],
            "TotalTax": "24.00",
            "Total": "240.00",
        }
    )

    result = reconcile(bill, order, chart_of_accounts=CHART, semantic_review_enabled=True)
    persist_reconciliation(
        conn,
        run_id=run_id,
        correlation_id=uuid.uuid4(),
        result=result,
        tolerance_version="v1",
        account_reference_version="chart-v1",
        account_reference_hash="c" * 64,
    )
    return run_id, result


def _good_response():
    return ProviderResult(
        raw={
            "recommendation": "LIKELY_EQUIVALENT",
            "confidence": 0.91,
            "explanation": "Both describe bottled water supplied in a case of 24.",
            "evidence": [
                {"source": "PURCHASE_ORDER_LINE", "text": "bottled"},
                {"source": "BILL_LINE", "text": "bottled water"},
            ],
        },
        model="fake-model-1",
    )


def test_a_clean_wording_case_records_a_recommendation():
    with psycopg.connect(OWNER_URL) as conn:
        run_id, recon = _seed_wording_run(conn)
        conn.commit()
        assert recon.semantic_permitted is True

        summary = semantic.review(
            conn,
            run_id=run_id,
            correlation_id=uuid.uuid4(),
            client=FakeClient(_good_response()),
            min_confidence=0.6,
            enabled_flag=True,
        )

        with conn.cursor() as cur:
            cur.execute(
                "SELECT semantic_stage_status, human_review_reasons FROM runs WHERE run_id=%s",
                (run_id,),
            )
            stage, reasons = cur.fetchone()
            cur.execute(
                "SELECT status, model_id, prompt_version, confidence "
                "FROM semantic_attempts WHERE run_id=%s",
                (run_id,),
            )
            status, model_id, prompt_version, confidence = cur.fetchone()

    assert summary["applied"] is True
    assert summary["recommendation"] == "LIKELY_EQUIVALENT"
    assert stage == "COMPLETED_WITH_RECOMMENDATION"
    assert "SEMANTIC_RECOMMENDATION_AVAILABLE" in reasons
    assert status == "SUCCEEDED"
    assert model_id == "fake-model-1"
    assert prompt_version == "line_semantics.v1"
    assert float(confidence) == 0.91


def test_the_outcome_is_never_altered_by_a_recommendation():
    """I02. The strongest assertion in this file."""
    with psycopg.connect(OWNER_URL) as conn:
        run_id, _ = _seed_wording_run(conn)
        conn.commit()
        semantic.review(
            conn,
            run_id=run_id,
            correlation_id=uuid.uuid4(),
            client=FakeClient(_good_response()),
            min_confidence=0.6,
            enabled_flag=True,
        )
        with conn.cursor() as cur:
            cur.execute(
                "SELECT reconciliation_outcome, triage_destination FROM runs WHERE run_id=%s",
                (run_id,),
            )
            outcome, destination = cur.fetchone()
    assert outcome == "REVIEW_REQUIRED"
    assert destination == "AP_REVIEW"


@pytest.mark.parametrize(
    ("provider", "expected_reason"),
    [
        (
            ProviderResult(rejection=Rejection(RejectionReason.REFUSED, "refusal")),
            "SEMANTIC_REFUSED",
        ),
        (
            ProviderResult(rejection=Rejection(RejectionReason.TRUNCATED, "max_tokens")),
            "SEMANTIC_TRUNCATED",
        ),
        (ProviderResult(raw={"recommendation": "LIKELY_EQUIVALENT"}), "SEMANTIC_OUTPUT_INVALID"),
        (
            ProviderResult(
                raw={
                    "recommendation": "LIKELY_EQUIVALENT",
                    "confidence": 0.9,
                    "explanation": "They look the same to me.",
                    "evidence": [{"source": "BILL_LINE", "text": "not in the text at all"}],
                }
            ),
            "SEMANTIC_EVIDENCE_UNSUPPORTED",
        ),
    ],
)
def test_every_failure_reaches_a_human_with_no_recommendation(provider, expected_reason):
    """I06. Six different failures, one destination."""
    with psycopg.connect(OWNER_URL) as conn:
        run_id, _ = _seed_wording_run(conn)
        conn.commit()
        summary = semantic.review(
            conn,
            run_id=run_id,
            correlation_id=uuid.uuid4(),
            client=FakeClient(provider),
            min_confidence=0.6,
            enabled_flag=True,
        )
        with conn.cursor() as cur:
            cur.execute(
                "SELECT semantic_stage_status, human_review_reasons, "
                "reconciliation_outcome FROM runs WHERE run_id=%s",
                (run_id,),
            )
            stage, reasons, outcome = cur.fetchone()

    assert summary["recommendation"] is None
    assert stage == "COMPLETED_WITHOUT_RECOMMENDATION"
    assert expected_reason in reasons
    assert outcome == "REVIEW_REQUIRED"


def test_low_confidence_shows_nothing_rather_than_a_hedge():
    with psycopg.connect(OWNER_URL) as conn:
        run_id, _ = _seed_wording_run(conn)
        conn.commit()
        weak = _good_response()
        weak.raw["confidence"] = 0.2
        summary = semantic.review(
            conn,
            run_id=run_id,
            correlation_id=uuid.uuid4(),
            client=FakeClient(weak),
            min_confidence=0.7,
            enabled_flag=True,
        )
        with conn.cursor() as cur:
            cur.execute("SELECT human_review_reasons FROM runs WHERE run_id=%s", (run_id,))
            reasons = cur.fetchone()[0]
    assert summary["recommendation"] is None
    assert "SEMANTIC_LOW_CONFIDENCE" in reasons


def test_a_closed_gate_refuses_before_any_call():
    """A residual pair with a price difference. The provider is never reached."""
    with psycopg.connect(OWNER_URL) as conn:
        run_id, recon = _seed_wording_run(
            conn, bill_over={"UnitAmount": "53.00", "LineAmount": "265.00"}
        )
        conn.commit()
        assert recon.semantic_permitted is False

        class Exploding:
            model = "must-not-be-called"

            def review(self, **_):
                raise AssertionError("the provider was called through a closed gate")

        with pytest.raises(semantic.GateRefusedError):
            semantic.review(
                conn,
                run_id=run_id,
                correlation_id=uuid.uuid4(),
                client=Exploding(),
                min_confidence=0.6,
                enabled_flag=True,
            )


def test_a_second_attempt_cannot_run_while_one_is_in_flight():
    """The partial unique index, not worker discipline."""
    with psycopg.connect(OWNER_URL) as conn:
        run_id, _ = _seed_wording_run(conn)
        conn.commit()
        semantic.start_attempt(
            conn, run_id=run_id, correlation_id=uuid.uuid4(), model_id="m", enabled_flag=True
        )
        conn.commit()
        with pytest.raises(psycopg.errors.UniqueViolation):
            semantic.start_attempt(
                conn, run_id=run_id, correlation_id=uuid.uuid4(), model_id="m", enabled_flag=True
            )


def test_the_captured_flag_is_recorded_with_the_attempt():
    """A later configuration change cannot reinterpret a run that already ran."""
    with psycopg.connect(OWNER_URL) as conn:
        run_id, _ = _seed_wording_run(conn)
        conn.commit()
        semantic.review(
            conn,
            run_id=run_id,
            correlation_id=uuid.uuid4(),
            client=FakeClient(_good_response()),
            min_confidence=0.6,
            enabled_flag=True,
        )
        with conn.cursor() as cur:
            cur.execute(
                "SELECT semantic_review_enabled_at_attempt FROM semantic_attempts WHERE run_id=%s",
                (run_id,),
            )
            assert cur.fetchone()[0] is True
