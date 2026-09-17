#!/usr/bin/env python3
"""The evaluation harness.

Two datasets, and they are scored differently on purpose.

`account_code_gate_v1` holds cases that must NEVER reach the model. Its result
is a correctness assertion, not a quality metric: a single leak is a bug.

`line_semantics_v1` holds wording cases that legitimately reach the model,
labelled with the answer a competent reviewer would give.

    python evaluations/run_eval.py            # gate only, no API calls
    python evaluations/run_eval.py --live     # also calls the provider
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from datetime import UTC, datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


from evaluations.metrics import Tally
from policy_service.domain.models import Bill, PurchaseOrder
from policy_service.domain.reconciliation import reconcile

HERE = pathlib.Path(__file__).resolve().parent
DATASETS = HERE / "datasets"
RESULTS = HERE / "results"


def load(name: str) -> list[dict]:
    path = DATASETS / f"{name}.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _documents(case: dict):
    """Build a bill and a purchase order that differ only as the case says."""

    def line(desc, **over):
        base = {
            "Description": desc,
            "AccountCode": case.get("account_code", "0010"),
            "TaxType": "INPUT",
            "Quantity": "5",
            "UnitAmount": "48.00",
            "LineAmount": "240.00",
            "TaxAmount": "24.00",
        }
        base.update(over)
        return base

    supplier = "11111111-1111-1111-1111-111111111111"
    bill = Bill.model_validate(
        {
            "InvoiceID": "22222222-2222-2222-2222-222222222222",
            "InvoiceNumber": "INV-EVAL",
            "Type": "ACCPAY",
            "Status": "DRAFT",
            "Reference": "PO-EVAL",
            "CurrencyCode": "AUD",
            "Contact": {"ContactID": supplier},
            "LineItems": [line(case["bill_line"], **case.get("bill_line_overrides", {}))],
            "TotalTax": "24.00",
            "Total": "240.00",
        }
    )
    order = PurchaseOrder.model_validate(
        {
            "PurchaseOrderNumber": "PO-EVAL",
            "Status": "AUTHORISED",
            "CurrencyCode": "AUD",
            "Contact": {"ContactID": supplier},
            "LineItems": [line(case["po_line"], **case.get("po_line_overrides", {}))],
            "TotalTax": "24.00",
            "Total": "240.00",
        }
    )
    return bill, order


def run_gate_dataset(tally: Tally) -> None:
    """Every case here must be excluded before any model call is constructed."""
    chart = frozenset({"0010", "0020"})
    for case in load("account_code_gate_v1"):
        bill, order = _documents(case)
        result = reconcile(bill, order, chart_of_accounts=chart, semantic_review_enabled=True)
        tally.record_gate_exclusion(case["id"], not result.semantic_permitted)


def run_semantics_dataset(tally: Tally, client, min_confidence: float) -> None:
    from policy_service.domain.normalisation import normalise_strict
    from policy_service.integrations.claude_contract import (
        ValidatedRecommendation,
        validate,
    )

    for case in load("line_semantics_v1"):
        po = normalise_strict(case["po_line"])
        bill = normalise_strict(case["bill_line"])
        provider = client.review(purchase_order_line_description=po, bill_line_description=bill)
        if provider.rejection is not None:
            tally.record(
                expected=case["expected"], actual=None, rejection=provider.rejection.reason.value
            )
            continue
        outcome = validate(
            provider.raw,
            purchase_order_line_description=po,
            bill_line_description=bill,
            min_confidence=min_confidence,
        )
        if isinstance(outcome, ValidatedRecommendation):
            tally.record(
                expected=case["expected"], actual=outcome.recommendation.value, rejection=None
            )
        else:
            tally.record(expected=case["expected"], actual=None, rejection=outcome.reason.value)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="also call the provider. Costs money.")
    args = parser.parse_args()

    tally = Tally()
    run_gate_dataset(tally)

    model = "gate-only"
    if args.live:
        from policy_service.config import get_settings
        from policy_service.integrations.claude_client import ClaudeClient

        settings = get_settings()
        if not settings.anthropic_api_key:
            print("ANTHROPIC_API_KEY is required for --live", file=sys.stderr)
            return 2
        client = ClaudeClient(api_key=settings.anthropic_api_key, model=settings.semantic_model_id)
        model = settings.semantic_model_id
        run_semantics_dataset(tally, client, settings.semantic_min_confidence)

    report = tally.report() | {
        "model_id": model,
        "prompt_version": "line_semantics.v1",
        "run_at": datetime.now(UTC).isoformat(),
    }
    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / f"eval-{datetime.now(UTC):%Y%m%dT%H%M%S}.json"
    out.write_text(json.dumps(report, indent=2) + "\n")

    print(json.dumps(report, indent=2))
    if not report["gate_exclusion_is_complete"]:
        print("\nGATE LEAK: a case that must never reach the model was permitted.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
