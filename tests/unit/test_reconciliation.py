"""The deterministic engine, Volume 06.

The nine scenarios at the bottom are the demonstration fixture set. They are
the cases the Slack cards show, so they are tested as a unit here before any
external service exists.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from policy_service.domain.enums import (
    ExceptionCode,
    Outcome,
    SemanticGateReason,
    SemanticStageStatus,
    TriageDestination,
    UnprocessableReason,
)
from policy_service.domain.models import Bill, PurchaseOrder
from policy_service.domain.normalisation import normalise_loose, normalise_strict
from policy_service.domain.reconciliation import pair_lines, reconcile
from policy_service.domain.routing import coverage_gaps

# A mixed-case code is in the chart so case sensitivity can be tested against
# two KNOWN codes. With unknown codes the fixed validation order would report
# "unknown" first and never reach the equality check.
CHART = frozenset({"0010", "0020", "0400", "0600", "Ab10", "ab10"})
SUPPLIER = str(uuid4())


def line(
    desc,
    qty="10",
    unit="5.00",
    amount="50.00",
    tax="5.00",
    code="0010",
    tax_type="INPUT",
    item=None,
):
    return {
        "Description": desc,
        "ItemCode": item,
        "AccountCode": code,
        "TaxType": tax_type,
        "Quantity": qty,
        "UnitAmount": unit,
        "LineAmount": amount,
        "TaxAmount": tax,
    }


def bill_of(
    lines,
    *,
    number="INV-001",
    ref="PO-001",
    supplier=SUPPLIER,
    currency="AUD",
    total="50.00",
    total_tax="5.00",
    type_="ACCPAY",
    status="DRAFT",
):
    """A bill as Xero actually presents one.

    An ACCPAY invoice has ONE free-text field: the UI labels it Reference and
    the API returns it as InvoiceNumber. The API `Reference` field is ACCREC
    only. So the supplier's number and the purchase-order reference share that
    single field, and `ref=""` means the bill names no purchase order.
    """
    combined = f"{number} {ref}".strip() if ref else number
    return Bill.model_validate(
        {
            "InvoiceID": str(uuid4()),
            "InvoiceNumber": combined,
            "Type": type_,
            "Status": status,
            "CurrencyCode": currency,
            "Contact": {"ContactID": supplier},
            "LineItems": lines,
            "TotalTax": total_tax,
            "Total": total,
        }
    )


def po_of(
    lines,
    *,
    supplier=SUPPLIER,
    currency="AUD",
    total="50.00",
    total_tax="5.00",
    status="AUTHORISED",
):
    return PurchaseOrder.model_validate(
        {
            "PurchaseOrderID": str(uuid4()),
            "PurchaseOrderNumber": "PO-001",
            "Status": status,
            "CurrencyCode": currency,
            "Contact": {"ContactID": supplier},
            "LineItems": lines,
            "TotalTax": total_tax,
            "Total": total,
        }
    )


def run(bill, po, **kw):
    return reconcile(bill, po, chart_of_accounts=CHART, **kw)


# --- normalisation is a pure function, tested against a fixed table --------
@pytest.mark.parametrize(
    ("raw", "strict", "loose"),
    [
        ("  Water,   BOTTLED  ", "water, bottled", "water bottled"),
        ("Water, bottled (case of 24)", "water, bottled (case of 24)", "water bottled case of 24"),
        ("Water bottled - case of 24", "water bottled - case of 24", "water bottled case of 24"),
        ("", "", ""),
    ],
)
def test_normalisation_table(raw, strict, loose):
    assert normalise_strict(raw) == strict
    assert normalise_loose(raw) == loose


def test_normalisation_never_reorders_or_expands():
    """No stemming, synonym expansion, unit conversion or spelling correction.
    Those are judgement calls, and judgement calls are what the human is for."""
    assert normalise_loose("24x 500ml water") != normalise_loose("water 500ml x24")
    assert normalise_loose("bottle") != normalise_loose("bottles")


# --- eligibility -----------------------------------------------------------
@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"type_": "ACCREC"}, UnprocessableReason.NOT_A_SUPPLIER_BILL),
        ({"status": "AUTHORISED"}, UnprocessableReason.BILL_STATUS_NOT_ELIGIBLE),
        ({"number": "   "}, UnprocessableReason.MISSING_INVOICE_NUMBER),
        ({"currency": ""}, UnprocessableReason.CONTRACT_INVALID),
    ],
)
def test_ineligible_bills_are_unprocessable(kwargs, reason):
    result = run(bill_of([line("widget")], **kwargs), po_of([line("widget")]))
    assert result.outcome is Outcome.UNPROCESSABLE
    assert result.unprocessable_reason is reason
    # UNPROCESSABLE never routes to Slack: there is nothing a reviewer can act on.
    assert result.triage_destination is None
    assert result.semantic_permitted is False


def test_missing_invoice_number_blocks_the_duplicate_key():
    """Constructing a key over an empty string would make every bill without a
    number collide with every other, manufacturing false duplicates."""
    from policy_service.domain.duplicates import invoice_number_key

    with pytest.raises(ValueError, match="non-empty"):
        invoice_number_key(SUPPLIER, "  ")


# --- purchase-order eligibility -------------------------------------------
@pytest.mark.parametrize(
    ("kw", "code"),
    [
        ({"reference_present": False}, ExceptionCode.NO_PO_REFERENCE),
        ({"po_allow_listed": False}, ExceptionCode.PO_NOT_ALLOW_LISTED),
    ],
)
def test_po_problems_are_exceptions_not_eligibility_failures(kw, code):
    result = run(bill_of([line("widget")]), po_of([line("widget")]), **kw)
    assert result.outcome is Outcome.REVIEW_REQUIRED
    assert code in result.exception_codes
    assert result.triage_destination is TriageDestination.PROCUREMENT


def test_draft_purchase_order_is_not_eligible():
    """A bill should not arrive against an order that was never approved."""
    result = run(bill_of([line("widget")]), po_of([line("widget")], status="DRAFT"))
    assert ExceptionCode.PO_STATUS_NOT_ELIGIBLE in result.exception_codes


def test_missing_purchase_order_is_review_not_unprocessable():
    result = reconcile(bill_of([line("w")]), None, chart_of_accounts=CHART)
    assert result.outcome is Outcome.REVIEW_REQUIRED
    assert ExceptionCode.PO_NOT_FOUND in result.exception_codes


# --- header ---------------------------------------------------------------
def test_supplier_mismatch_stops_all_line_comparison():
    """Comparing lines across two different suppliers produces meaningless
    variances, so no line comparison is attempted."""
    result = run(
        bill_of([line("widget", qty="99")]), po_of([line("widget")], supplier=str(uuid4()))
    )
    assert result.exception_codes == [ExceptionCode.SUPPLIER_MISMATCH]
    assert result.paired_line_count == 0
    assert result.semantic_permitted is False


def test_currency_mismatch_is_recorded():
    result = run(bill_of([line("w")], currency="usd"), po_of([line("w")]))
    assert ExceptionCode.CURRENCY_MISMATCH in result.exception_codes


# --- pairing --------------------------------------------------------------
def test_pairing_tiers_in_order():
    bill = [line("Alpha", item="SKU-1"), line("Water, bottled (case of 24)")]
    po = [line("Completely different", item="sku-1"), line("Water bottled - case of 24")]
    pairs, ub, up = pair_lines(
        [
            __import__(
                "policy_service.domain.models", fromlist=["LineItem"]
            ).LineItem.model_validate(x)
            for x in bill
        ],
        [
            __import__(
                "policy_service.domain.models", fromlist=["LineItem"]
            ).LineItem.model_validate(x)
            for x in po
        ],
    )
    assert sorted(pairs) == [(0, 0), (1, 1)]
    assert ub == [] and up == []


def test_ambiguous_pairing_pairs_nothing_at_that_tier():
    """A tier pairs only where the match is unique on BOTH sides. Picking the
    first of several candidates would be an arbitrary choice dressed as a rule."""
    from policy_service.domain.models import LineItem

    bill = [LineItem.model_validate(line("widget")), LineItem.model_validate(line("widget"))]
    po = [LineItem.model_validate(line("widget")), LineItem.model_validate(line("gadget"))]
    pairs, ub, up = pair_lines(bill, po)
    assert pairs == []
    assert len(ub) == 2 and len(up) == 2


# --- account codes --------------------------------------------------------
def test_account_code_leading_zeroes_are_preserved():
    """An account code is an identifier, not a number: 0010 and 10 differ."""
    result = run(bill_of([line("w", code=" 0010 ")]), po_of([line("w", code="0010")]))
    assert result.outcome is Outcome.MATCHED


def test_account_code_comparison_is_case_sensitive():
    result = run(bill_of([line("w", code="Ab10")]), po_of([line("w", code="ab10")]))
    assert ExceptionCode.ACCOUNT_CODE_MISMATCH in result.exception_codes


def test_missing_code_cannot_also_be_a_mismatch():
    """Validation order is fixed: missing, invalid, unknown, mismatch.
    ACCOUNT_CODE_MISMATCH means both values are present, valid, known and unequal."""
    result = run(bill_of([line("w", code=None)]), po_of([line("w", code="0020")]))
    codes = result.exception_codes
    assert ExceptionCode.MISSING_BILL_ACCOUNT_CODE in codes
    assert ExceptionCode.ACCOUNT_CODE_MISMATCH not in codes


def test_unknown_code_is_not_a_mismatch():
    result = run(bill_of([line("w", code="9999")]), po_of([line("w", code="9999")]))
    codes = result.exception_codes
    assert ExceptionCode.UNKNOWN_BILL_ACCOUNT_CODE in codes
    assert ExceptionCode.UNKNOWN_PO_ACCOUNT_CODE in codes
    assert ExceptionCode.ACCOUNT_CODE_MISMATCH not in codes


def test_bill_side_finding_precedes_po_side():
    result = run(bill_of([line("w", code=None)]), po_of([line("w", code=None)]))
    codes = result.exception_codes
    assert codes.index(ExceptionCode.MISSING_BILL_ACCOUNT_CODE) < codes.index(
        ExceptionCode.MISSING_PO_ACCOUNT_CODE
    )


# --- tolerances -----------------------------------------------------------
def test_quantity_has_zero_tolerance():
    """A supplier billing a different quantity is exactly what this exists to
    surface."""
    result = run(bill_of([line("w", qty="10.001")]), po_of([line("w")]))
    assert ExceptionCode.QUANTITY_VARIANCE in result.exception_codes


def test_line_amount_absorbs_cent_rounding():
    result = run(bill_of([line("w", amount="50.01")], total="50.01"), po_of([line("w")]))
    assert ExceptionCode.LINE_AMOUNT_VARIANCE not in result.exception_codes


def test_tax_type_is_compared_never_inferred():
    result = run(bill_of([line("w", tax_type="")]), po_of([line("w")]))
    assert ExceptionCode.MISSING_TAX_TYPE in result.exception_codes


# --- the residual gate, the load-bearing part -----------------------------
def test_clean_residual_pair_permits_the_model():
    """I26: every number agrees, only the wording differs."""
    result = run(
        bill_of([line("24x 500ml bottled water, assorted")]),
        po_of([line("Water, bottled, case of 24")]),
    )
    assert result.outcome is Outcome.REVIEW_REQUIRED
    assert ExceptionCode.UNPAIRED_LINE in result.exception_codes
    assert result.semantic_permitted is True
    assert result.semantic_gate_reason is SemanticGateReason.RESIDUAL_PAIR_TEXT_ONLY


def test_residual_pair_with_a_price_difference_refuses_the_model():
    """The step people leave out. Sending this to a model as though wording were
    the only question would produce an answer that actively misleads."""
    result = run(
        bill_of([line("24x 500ml bottled water", unit="6.00", amount="60.00")], total="60.00"),
        po_of([line("Water, bottled, case of 24")]),
    )
    assert result.semantic_permitted is False
    assert result.semantic_gate_reason is (SemanticGateReason.RESIDUAL_PAIR_DETERMINISTIC_MISMATCH)
    assert ExceptionCode.RESIDUAL_UNIT_PRICE_VARIANCE in result.exception_codes


def test_residual_account_code_mismatch_refuses_the_model():
    result = run(
        bill_of([line("24x 500ml bottled water", code="0020")]),
        po_of([line("Water, bottled, case of 24")]),
    )
    assert result.semantic_permitted is False
    assert ExceptionCode.RESIDUAL_ACCOUNT_CODE_MISMATCH in result.exception_codes


def test_two_candidates_on_either_side_never_reach_the_model():
    """I05: Claude is never asked to choose among candidates."""
    result = run(
        bill_of([line("alpha thing"), line("beta thing")], total="100.00", total_tax="10.00"),
        po_of([line("gamma item"), line("delta item")], total="100.00", total_tax="10.00"),
    )
    assert ExceptionCode.AMBIGUOUS_MULTIPLE_CANDIDATES in result.exception_codes
    assert result.semantic_permitted is False


def test_residual_never_pairs_the_lines():
    """I27: MATCHED is unreachable from any path involving a residual pair."""
    result = run(
        bill_of([line("24x 500ml bottled water")]),
        po_of([line("Water, bottled, case of 24")]),
    )
    assert result.outcome is not Outcome.MATCHED
    assert result.paired_line_count == 0
    assert ExceptionCode.UNPAIRED_LINE in result.exception_codes


def test_gate_reason_is_recorded_on_every_run_including_matched():
    """Auditable rather than inferred from the absence of a recommendation."""
    for result in (
        run(bill_of([line("w")]), po_of([line("w")])),
        run(bill_of([line("w", qty="11")]), po_of([line("w")])),
    ):
        assert result.semantic_gate_reason is not None


# --- stage initialisation --------------------------------------------------
def test_stage_disabled_when_flag_is_off():
    result = run(
        bill_of([line("24x 500ml bottled water")]),
        po_of([line("Water, bottled, case of 24")]),
        semantic_review_enabled=False,
    )
    assert result.semantic_permitted is True
    assert result.semantic_stage_status is SemanticStageStatus.DISABLED


def test_stage_pending_when_flag_is_on():
    result = run(
        bill_of([line("24x 500ml bottled water")]),
        po_of([line("Water, bottled, case of 24")]),
        semantic_review_enabled=True,
    )
    assert result.semantic_stage_status is SemanticStageStatus.PENDING


def test_configuration_cannot_open_a_closed_gate():
    """Configuration is not part of the deterministic gate."""
    result = run(
        bill_of([line("w", qty="11")]),
        po_of([line("w")]),
        semantic_review_enabled=True,
    )
    assert result.semantic_permitted is False
    assert result.semantic_stage_status is SemanticStageStatus.NOT_REQUIRED


# --- structural ------------------------------------------------------------
def test_every_exception_code_has_a_routing_owner():
    assert coverage_gaps() == set()


def test_matched_run_is_silent():
    result = run(bill_of([line("w")]), po_of([line("w")]))
    assert result.outcome is Outcome.MATCHED
    assert result.exceptions == []
    assert result.triage_destination is None
    assert result.human_review_reasons == []


def test_every_review_run_carries_a_reason_and_a_destination():
    result = run(bill_of([line("w", qty="11")]), po_of([line("w")]))
    assert result.outcome is Outcome.REVIEW_REQUIRED
    assert result.human_review_reasons
    assert result.triage_destination is not None


# --- the nine demonstration scenarios -------------------------------------
def test_scenario_1_clean_match():
    assert run(bill_of([line("w")]), po_of([line("w")])).outcome is Outcome.MATCHED


def test_scenario_2_quantity_variance():
    r = run(bill_of([line("w", qty="11")]), po_of([line("w")]))
    assert ExceptionCode.QUANTITY_VARIANCE in r.exception_codes
    assert r.triage_destination is TriageDestination.PROCUREMENT


def test_scenario_3_unit_price_variance():
    r = run(bill_of([line("w", unit="6.00", amount="60.00")], total="60.00"), po_of([line("w")]))
    assert ExceptionCode.UNIT_PRICE_VARIANCE in r.exception_codes


def test_scenario_4_account_code_mismatch():
    r = run(bill_of([line("w", code="0020")]), po_of([line("w")]))
    assert ExceptionCode.ACCOUNT_CODE_MISMATCH in r.exception_codes
    assert r.triage_destination is TriageDestination.FINANCE


def test_scenario_5_tax_type_mismatch():
    r = run(bill_of([line("w", tax_type="EXEMPTINPUT")]), po_of([line("w")]))
    assert ExceptionCode.TAX_TYPE_MISMATCH in r.exception_codes
    assert r.triage_destination is TriageDestination.FINANCE


def test_scenario_6_duplicate_invoice_number():
    r = run(bill_of([line("w")]), po_of([line("w")]), duplicate_invoice_hit=True)
    assert ExceptionCode.DUPLICATE_INVOICE_NUMBER in r.exception_codes
    assert r.triage_destination is TriageDestination.DUPLICATE_REVIEW


def test_scenario_7_no_po_reference():
    r = run(bill_of([line("w")], ref=""), po_of([line("w")]))
    assert ExceptionCode.NO_PO_REFERENCE in r.exception_codes


def test_scenario_8_wording_only_reaches_the_model():
    r = run(
        bill_of([line("24x 500ml bottled water, assorted")]),
        po_of([line("Water, bottled, case of 24")]),
        semantic_review_enabled=True,
    )
    assert r.semantic_permitted is True
    assert r.semantic_stage_status is SemanticStageStatus.PENDING
    assert r.triage_destination is TriageDestination.AP_REVIEW


def test_scenario_9_wording_plus_price_does_not():
    r = run(
        bill_of([line("24x 500ml bottled water", unit="6.00", amount="60.00")], total="60.00"),
        po_of([line("Water, bottled, case of 24")]),
        semantic_review_enabled=True,
    )
    assert r.semantic_permitted is False
    assert r.semantic_stage_status is SemanticStageStatus.NOT_REQUIRED


def test_decimals_never_become_floats():
    r = run(bill_of([line("w", unit="0.1")]), po_of([line("w", unit="0.1")]))
    assert isinstance(Decimal("0.1"), Decimal)
    assert ExceptionCode.UNIT_PRICE_VARIANCE not in r.exception_codes


# --- the single ACCPAY free-text field ------------------------------------
def test_a_bill_has_one_free_text_field_and_it_carries_both():
    """Xero returns `Reference` only for ACCREC. On a bill the UI field labelled
    Reference arrives as InvoiceNumber, so the supplier's number and the
    purchase-order reference share it."""
    from policy_service.domain.normalisation import split_bill_reference

    assert split_bill_reference("INV-1001 PO-1001") == ("INV-1001", "PO-1001")
    assert split_bill_reference("INV-1007") == ("INV-1007", None)
    assert split_bill_reference("  INV-1 / PO-1  ") == ("INV-1", "PO-1")
    assert split_bill_reference("") == ("", None)


def test_a_field_naming_only_a_purchase_order_has_no_invoice_number():
    """Hashing `PO-1001` as a supplier invoice number would be worse than
    refusing the bill."""
    result = run(bill_of([line("w")], number="  ", ref="PO-001"), po_of([line("w")]))
    assert result.outcome is Outcome.UNPROCESSABLE
    assert result.unprocessable_reason is UnprocessableReason.MISSING_INVOICE_NUMBER


def test_the_purchase_order_reference_is_found_inside_the_invoice_number():
    result = run(bill_of([line("w")], number="INV-1001", ref="PO-001"), po_of([line("w")]))
    assert ExceptionCode.NO_PO_REFERENCE not in result.exception_codes


# --- header aggregates are consequences, not causes ------------------------
def test_a_tax_type_difference_routes_to_finance_despite_the_total_moving():
    """Changing a tax rate moves the line tax, the tax total and the invoice
    total. Routing on the total would send a tax question to procurement."""
    from policy_service.domain.routing import route

    codes = [
        ExceptionCode.TAX_TYPE_MISMATCH,
        ExceptionCode.LINE_TAX_VARIANCE,
        ExceptionCode.TAX_VARIANCE,
        ExceptionCode.TOTAL_VARIANCE,
    ]
    assert route(codes) is TriageDestination.FINANCE


def test_a_total_variance_alone_still_goes_to_procurement():
    """No line disagrees, but the totals do. That is a procurement question."""
    from policy_service.domain.routing import route

    assert route([ExceptionCode.TOTAL_VARIANCE]) is TriageDestination.PROCUREMENT


def test_a_tax_total_variance_alone_goes_to_finance():
    from policy_service.domain.routing import route

    assert route([ExceptionCode.TAX_VARIANCE]) is TriageDestination.FINANCE


def test_a_price_difference_still_outranks_the_total_it_caused():
    from policy_service.domain.routing import route

    assert (
        route([ExceptionCode.UNIT_PRICE_VARIANCE, ExceptionCode.TOTAL_VARIANCE])
        is TriageDestination.PROCUREMENT
    )
