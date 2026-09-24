"""The deterministic reconciliation engine (I04).

Takes a bill, a purchase order and a chart-of-accounts set. Returns a complete
decision. No I/O, no database, no clock, no configuration lookup: everything it
needs arrives as an argument, which is why it can be tested exhaustively.

Order of work, and it matters:

    1. bill eligibility          -> UNPROCESSABLE, stops here
    2. duplicate keys
    3. purchase-order eligibility -> REVIEW_REQUIRED, stops line work
    4. header checks
    5. line pairing, three tiers
    6. per-pair checks
    7. header monetary checks
    8. residual-pair provisional comparison
    9. the semantic gate result
   10. outcome, routing, review reasons, stage initialisation

Step 8 is the one people leave out, and leaving it out is what makes an AI
recommendation dangerous. See invariants I26 and I27.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from policy_service.domain.enums import (
    SEMANTIC_BLOCKING_CODES,
    ExceptionCode,
    HumanReviewReason,
    Outcome,
    SemanticGateReason,
    SemanticStageStatus,
    TriageDestination,
    UnprocessableReason,
)
from policy_service.domain.models import Bill, LineItem, PurchaseOrder, Tolerances
from policy_service.domain.normalisation import (
    account_code_format_valid,
    canonical_account_code,
    normalise_currency,
    normalise_loose,
    normalise_strict,
    normalise_tax_type,
    split_bill_reference,
    trim,
    within_tolerance,
)
from policy_service.domain.routing import route

# Upper bound on each normalised residual description sent to a model. An
# over-long or empty description closes the gate rather than being truncated,
# because truncating changes the thing being compared.
MAX_RESIDUAL_DESCRIPTION_CHARS = 500

ELIGIBLE_BILL_TYPE = "ACCPAY"
ELIGIBLE_BILL_STATUS = "DRAFT"
# DRAFT and SUBMITTED are not eligible: a bill should not arrive against an
# order that was never approved.
ELIGIBLE_PO_STATUSES = frozenset({"AUTHORISED", "BILLED"})


@dataclass(frozen=True)
class ExceptionItem:
    code: ExceptionCode
    line_reference: str  # a line index, "header", or "residual"
    detail: dict[str, str] = field(default_factory=dict)


@dataclass
class AccountCodeEvidence:
    """Explicit fields rather than a vague validity flag."""

    bill_account_code_raw: str | None = None
    po_account_code_raw: str | None = None
    bill_account_code_canonical: str | None = None
    po_account_code_canonical: str | None = None
    bill_account_code_present: bool = False
    po_account_code_present: bool = False
    bill_account_code_format_valid: bool = False
    po_account_code_format_valid: bool = False
    bill_account_code_known: bool = False
    po_account_code_known: bool = False
    account_codes_equal: bool = False
    exception_code: str | None = None


@dataclass
class ReconciliationResult:
    outcome: Outcome
    unprocessable_reason: UnprocessableReason | None = None
    exceptions: list[ExceptionItem] = field(default_factory=list)
    paired_line_count: int = 0
    unpaired_bill_lines: list[int] = field(default_factory=list)
    unpaired_po_lines: list[int] = field(default_factory=list)
    residual_comparison: dict | None = None
    semantic_permitted: bool = False
    semantic_gate_reason: SemanticGateReason = SemanticGateReason.OUTCOME_NOT_REVIEW_REQUIRED
    semantic_stage_status: SemanticStageStatus = SemanticStageStatus.NOT_REQUIRED
    triage_destination: TriageDestination | None = None
    human_review_reasons: list[HumanReviewReason] = field(default_factory=list)
    duplicate_invoice_key: str | None = None
    duplicate_business_key: str | None = None

    @property
    def exception_codes(self) -> list[ExceptionCode]:
        return [item.code for item in self.exceptions]


# --------------------------------------------------------------------------
# 1. eligibility
# --------------------------------------------------------------------------
def check_bill_eligibility(bill: Bill) -> UnprocessableReason | None:
    """Bill eligibility. Failing any condition gives UNPROCESSABLE."""
    if (bill.type or "").upper() != ELIGIBLE_BILL_TYPE:
        return UnprocessableReason.NOT_A_SUPPLIER_BILL
    if (bill.status or "").upper() != ELIGIBLE_BILL_STATUS:
        return UnprocessableReason.BILL_STATUS_NOT_ELIGIBLE
    if bill.invoice_id is None:
        return UnprocessableReason.CONTRACT_INVALID
    if bill.contact is None or bill.contact.contact_id is None:
        return UnprocessableReason.CONTRACT_INVALID
    if not bill.line_items:
        return UnprocessableReason.CONTRACT_INVALID
    if not trim(bill.currency_code):
        return UnprocessableReason.CONTRACT_INVALID
    # Checked last among contract rules because it has its own named reason:
    # the duplicate key cannot be constructed without it.
    #
    # The PARSED number, not the raw field. A bill whose single free-text field
    # contains only a purchase-order reference has no supplier invoice number,
    # and hashing "PO-1001" as one would be worse than refusing it.
    if not split_bill_reference(bill.invoice_number)[0]:
        return UnprocessableReason.MISSING_INVOICE_NUMBER
    return None


def check_po_eligibility(
    purchase_order: PurchaseOrder | None,
    *,
    reference_present: bool,
    allow_listed: bool,
) -> ExceptionCode | None:
    """Purchase-order checks. These are exceptions, not eligibility failures: a human can
    act on a missing purchase order."""
    if not reference_present:
        return ExceptionCode.NO_PO_REFERENCE
    if purchase_order is None:
        return ExceptionCode.PO_NOT_FOUND
    if not allow_listed:
        return ExceptionCode.PO_NOT_ALLOW_LISTED
    if (purchase_order.status or "").upper() not in ELIGIBLE_PO_STATUSES:
        return ExceptionCode.PO_STATUS_NOT_ELIGIBLE
    return None


# --------------------------------------------------------------------------
# 5. pairing
# --------------------------------------------------------------------------
def _unique_match(keys_left: dict[int, str], keys_right: dict[int, str]) -> list[tuple[int, int]]:
    """Pair only where the key is unique on BOTH sides.

    Anything that would produce a one-to-many or many-to-many match pairs
    nothing at this tier and falls through. Picking the first of several
    candidates would be an arbitrary choice dressed up as a rule.
    """
    from collections import Counter

    left_counts = Counter(keys_left.values())
    right_counts = Counter(keys_right.values())
    right_by_key = {v: k for k, v in keys_right.items()}

    pairs = []
    for left_index, key in keys_left.items():
        if not key:
            continue
        if left_counts[key] == 1 and right_counts.get(key, 0) == 1:
            pairs.append((left_index, right_by_key[key]))
    return pairs


def pair_lines(
    bill_lines: list[LineItem], po_lines: list[LineItem]
) -> tuple[list[tuple[int, int]], list[int], list[int]]:
    """Line pairing. Three tiers, in order, one-to-one at every tier."""
    remaining_bill = set(range(len(bill_lines)))
    remaining_po = set(range(len(po_lines)))
    pairs: list[tuple[int, int]] = []

    tiers = (
        lambda line: trim(line.item_code).casefold(),  # tier 1: ItemCode
        lambda line: normalise_strict(line.description),  # tier 2: strict
        lambda line: normalise_loose(line.description),  # tier 3: loose
    )

    for key_of in tiers:
        left = {i: key_of(bill_lines[i]) for i in sorted(remaining_bill)}
        right = {i: key_of(po_lines[i]) for i in sorted(remaining_po)}
        for bill_index, po_index in _unique_match(left, right):
            pairs.append((bill_index, po_index))
            remaining_bill.discard(bill_index)
            remaining_po.discard(po_index)

    return pairs, sorted(remaining_bill), sorted(remaining_po)


# --------------------------------------------------------------------------
# account codes
# --------------------------------------------------------------------------
_ACCOUNT_CODES = {
    "paired": {
        "missing_bill": ExceptionCode.MISSING_BILL_ACCOUNT_CODE,
        "missing_po": ExceptionCode.MISSING_PO_ACCOUNT_CODE,
        "invalid_bill": ExceptionCode.INVALID_BILL_ACCOUNT_CODE,
        "invalid_po": ExceptionCode.INVALID_PO_ACCOUNT_CODE,
        "unknown_bill": ExceptionCode.UNKNOWN_BILL_ACCOUNT_CODE,
        "unknown_po": ExceptionCode.UNKNOWN_PO_ACCOUNT_CODE,
        "mismatch": ExceptionCode.ACCOUNT_CODE_MISMATCH,
    },
    "residual": {
        "missing_bill": ExceptionCode.RESIDUAL_MISSING_BILL_ACCOUNT_CODE,
        "missing_po": ExceptionCode.RESIDUAL_MISSING_PO_ACCOUNT_CODE,
        "invalid_bill": ExceptionCode.RESIDUAL_INVALID_BILL_ACCOUNT_CODE,
        "invalid_po": ExceptionCode.RESIDUAL_INVALID_PO_ACCOUNT_CODE,
        "unknown_bill": ExceptionCode.RESIDUAL_UNKNOWN_BILL_ACCOUNT_CODE,
        "unknown_po": ExceptionCode.RESIDUAL_UNKNOWN_PO_ACCOUNT_CODE,
        "mismatch": ExceptionCode.RESIDUAL_ACCOUNT_CODE_MISMATCH,
    },
}


def compare_account_codes(
    bill_line: LineItem,
    po_line: LineItem,
    chart: frozenset[str],
    variant: str = "paired",
) -> tuple[list[ExceptionCode], AccountCodeEvidence]:
    """Validation order is FIXED: missing, invalid format, unknown, mismatch.
    Within each category the bill side precedes the purchase-order side.

    Equality is evaluated only after both sides pass presence, format and
    known-code validation. A pair with a missing or malformed value cannot also
    be classified as a mismatch: ACCOUNT_CODE_MISMATCH means both canonical
    values are present, format-valid, known and unequal.

    Claude is never asked whether two account codes are equivalent.
    """
    codes = _ACCOUNT_CODES[variant]
    ev = AccountCodeEvidence(
        bill_account_code_raw=bill_line.account_code,
        po_account_code_raw=po_line.account_code,
    )
    ev.bill_account_code_canonical = canonical_account_code(bill_line.account_code)
    ev.po_account_code_canonical = canonical_account_code(po_line.account_code)
    ev.bill_account_code_present = ev.bill_account_code_canonical is not None
    ev.po_account_code_present = ev.po_account_code_canonical is not None
    ev.bill_account_code_format_valid = account_code_format_valid(ev.bill_account_code_canonical)
    ev.po_account_code_format_valid = account_code_format_valid(ev.po_account_code_canonical)
    ev.bill_account_code_known = (
        ev.bill_account_code_format_valid and ev.bill_account_code_canonical in chart
    )
    ev.po_account_code_known = (
        ev.po_account_code_format_valid and ev.po_account_code_canonical in chart
    )

    found: list[ExceptionCode] = []
    if not ev.bill_account_code_present:
        found.append(codes["missing_bill"])
    if not ev.po_account_code_present:
        found.append(codes["missing_po"])
    if ev.bill_account_code_present and not ev.bill_account_code_format_valid:
        found.append(codes["invalid_bill"])
    if ev.po_account_code_present and not ev.po_account_code_format_valid:
        found.append(codes["invalid_po"])
    if ev.bill_account_code_format_valid and not ev.bill_account_code_known:
        found.append(codes["unknown_bill"])
    if ev.po_account_code_format_valid and not ev.po_account_code_known:
        found.append(codes["unknown_po"])

    if not found:
        # Case-SENSITIVE. The recorded chart code is the authoritative
        # identifier; no case folding, numeric conversion, substring, fuzzy or
        # semantic comparison is permitted.
        ev.account_codes_equal = ev.bill_account_code_canonical == ev.po_account_code_canonical
        if not ev.account_codes_equal:
            found.append(codes["mismatch"])

    ev.exception_code = found[0].value if found else None
    return found, ev


# --------------------------------------------------------------------------
# 6 and 8. line comparison, used identically for paired and residual lines
# --------------------------------------------------------------------------
_LINE_CODES = {
    "paired": {
        "quantity": ExceptionCode.QUANTITY_VARIANCE,
        "unit_price": ExceptionCode.UNIT_PRICE_VARIANCE,
        "line_amount": ExceptionCode.LINE_AMOUNT_VARIANCE,
        "tax_type": ExceptionCode.TAX_TYPE_MISMATCH,
        "missing_tax_type": ExceptionCode.MISSING_TAX_TYPE,
        "line_tax": ExceptionCode.LINE_TAX_VARIANCE,
    },
    "residual": {
        "quantity": ExceptionCode.RESIDUAL_QUANTITY_VARIANCE,
        "unit_price": ExceptionCode.RESIDUAL_UNIT_PRICE_VARIANCE,
        "line_amount": ExceptionCode.RESIDUAL_LINE_AMOUNT_VARIANCE,
        "tax_type": ExceptionCode.RESIDUAL_TAX_TYPE_MISMATCH,
        "missing_tax_type": ExceptionCode.RESIDUAL_MISSING_TAX_TYPE,
        "line_tax": ExceptionCode.RESIDUAL_LINE_TAX_VARIANCE,
    },
}


def compare_line(
    bill_line: LineItem,
    po_line: LineItem,
    tolerances: Tolerances,
    chart: frozenset[str],
    variant: str = "paired",
) -> tuple[list[ExceptionCode], AccountCodeEvidence]:
    """Line comparison and account-code checks.

    The SAME function and the SAME tolerances are applied to paired lines and
    to the provisional residual pair. That identity is the whole point: it is
    what lets the gate claim the residual pair differs only in wording.
    """
    codes = _LINE_CODES[variant]
    found: list[ExceptionCode] = []

    if not within_tolerance(bill_line.quantity, po_line.quantity, tolerances.quantity_abs):
        found.append(codes["quantity"])
    if not within_tolerance(bill_line.unit_amount, po_line.unit_amount, tolerances.unit_price_abs):
        found.append(codes["unit_price"])
    if not within_tolerance(bill_line.line_amount, po_line.line_amount, tolerances.line_amount_abs):
        found.append(codes["line_amount"])

    # Tax is COMPARED, never computed. If either side omits TaxType that is a
    # missing-tax-type exception, not an inference.
    bill_tax_type = normalise_tax_type(bill_line.tax_type)
    po_tax_type = normalise_tax_type(po_line.tax_type)
    if not bill_tax_type or not po_tax_type:
        found.append(codes["missing_tax_type"])
    elif bill_tax_type != po_tax_type:
        found.append(codes["tax_type"])

    if not within_tolerance(bill_line.tax_amount, po_line.tax_amount, tolerances.tax_abs):
        found.append(codes["line_tax"])

    account_found, evidence = compare_account_codes(bill_line, po_line, chart, variant)
    found.extend(account_found)
    return found, evidence


# --------------------------------------------------------------------------
# 10. the whole decision
# --------------------------------------------------------------------------
def reconcile(
    bill: Bill,
    purchase_order: PurchaseOrder | None,
    *,
    chart_of_accounts: frozenset[str],
    tolerances: Tolerances | None = None,
    reference_present: bool | None = None,
    po_allow_listed: bool = True,
    duplicate_invoice_key: str | None = None,
    duplicate_business_key: str | None = None,
    duplicate_invoice_hit: bool = False,
    duplicate_business_hit: bool = False,
    semantic_review_enabled: bool = False,
) -> ReconciliationResult:
    """Reconcile one bill against one purchase order.

    Duplicate LOOKUPS happen in the repository, not here: this function stays
    pure. The caller passes the keys and whether each collided.
    """
    tolerances = tolerances or Tolerances()

    # 1. eligibility. UNPROCESSABLE never reaches Slack and stops here.
    reason = check_bill_eligibility(bill)
    if reason is not None:
        return ReconciliationResult(
            outcome=Outcome.UNPROCESSABLE,
            unprocessable_reason=reason,
            semantic_gate_reason=SemanticGateReason.OUTCOME_NOT_REVIEW_REQUIRED,
            semantic_stage_status=SemanticStageStatus.NOT_REQUIRED,
        )

    result = ReconciliationResult(outcome=Outcome.REVIEW_REQUIRED)
    result.duplicate_invoice_key = duplicate_invoice_key
    result.duplicate_business_key = duplicate_business_key
    exceptions: list[ExceptionItem] = []

    # 2. duplicates
    if duplicate_invoice_hit:
        exceptions.append(ExceptionItem(ExceptionCode.DUPLICATE_INVOICE_NUMBER, "header"))
    if duplicate_business_hit:
        exceptions.append(ExceptionItem(ExceptionCode.DUPLICATE_BUSINESS_KEY, "header"))

    # 3. purchase-order eligibility. No purchase order means no line work.
    if reference_present is None:
        # An ACCPAY bill has one free-text field, so the purchase-order
        # reference lives inside the invoice number. See split_bill_reference.
        reference_present = split_bill_reference(bill.invoice_number)[1] is not None
    po_problem = check_po_eligibility(
        purchase_order, reference_present=reference_present, allow_listed=po_allow_listed
    )
    if po_problem is not None:
        exceptions.append(ExceptionItem(po_problem, "header"))
        return _finalise(result, exceptions, semantic_review_enabled)

    assert purchase_order is not None  # narrowed by check_po_eligibility

    # 4. header checks. A supplier mismatch is TERMINAL for reconciliation:
    # comparing lines across two different suppliers produces meaningless
    # variances, so no line comparison is attempted.
    bill_contact = bill.contact.contact_id if bill.contact else None
    po_contact = purchase_order.contact.contact_id if purchase_order.contact else None
    if bill_contact != po_contact:
        exceptions.append(ExceptionItem(ExceptionCode.SUPPLIER_MISMATCH, "header"))
        return _finalise(result, exceptions, semantic_review_enabled)

    if normalise_currency(bill.currency_code) != normalise_currency(purchase_order.currency_code):
        exceptions.append(ExceptionItem(ExceptionCode.CURRENCY_MISMATCH, "header"))

    # 5. pairing
    pairs, unpaired_bill, unpaired_po = pair_lines(bill.line_items, purchase_order.line_items)
    result.paired_line_count = len(pairs)
    result.unpaired_bill_lines = unpaired_bill
    result.unpaired_po_lines = unpaired_po

    # 6. per-pair checks
    for bill_index, po_index in pairs:
        found, _ = compare_line(
            bill.line_items[bill_index],
            purchase_order.line_items[po_index],
            tolerances,
            chart_of_accounts,
            "paired",
        )
        for code in found:
            exceptions.append(
                ExceptionItem(
                    code,
                    str(bill_index),
                    {"bill_line": str(bill_index), "po_line": str(po_index)},
                )
            )

    # 7. header monetary
    if not within_tolerance(bill.total_tax, purchase_order.total_tax, tolerances.tax_abs):
        exceptions.append(ExceptionItem(ExceptionCode.TAX_VARIANCE, "header"))
    if not within_tolerance(bill.total, purchase_order.total, tolerances.total_abs):
        exceptions.append(ExceptionItem(ExceptionCode.TOTAL_VARIANCE, "header"))

    # line-count and unpaired lines
    if unpaired_bill or unpaired_po:
        if len(bill.line_items) != len(purchase_order.line_items):
            exceptions.append(ExceptionItem(ExceptionCode.LINE_COUNT_MISMATCH, "header"))
        exceptions.append(ExceptionItem(ExceptionCode.UNPAIRED_LINE, "header"))

    # 8. the provisional residual comparison.
    #
    # This is the step that makes a model recommendation safe. Checking numeric
    # fields only on lines that ALREADY paired does not prove the remaining
    # uncertainty is limited to wording: two lines can fail to pair on
    # description and also disagree on price.
    gate_reason = SemanticGateReason.NO_RESIDUAL_PAIR
    residual_clean = False

    if len(unpaired_bill) > 1 or len(unpaired_po) > 1:
        # I05: two or more candidates on either side. Claude is never asked to
        # choose among candidates.
        exceptions.append(ExceptionItem(ExceptionCode.AMBIGUOUS_MULTIPLE_CANDIDATES, "header"))
        gate_reason = SemanticGateReason.MULTIPLE_RESIDUAL_CANDIDATES

    elif len(unpaired_bill) == 1 and len(unpaired_po) == 1:
        bill_index, po_index = unpaired_bill[0], unpaired_po[0]
        bill_line = bill.line_items[bill_index]
        po_line = purchase_order.line_items[po_index]
        found, evidence = compare_line(
            bill_line, po_line, tolerances, chart_of_accounts, "residual"
        )
        for code in found:
            exceptions.append(ExceptionItem(code, "residual"))

        bill_text = normalise_strict(bill_line.description)
        po_text = normalise_strict(po_line.description)
        descriptions_in_bounds = all(
            0 < len(text) <= MAX_RESIDUAL_DESCRIPTION_CHARS for text in (bill_text, po_text)
        )

        residual_clean = not found and descriptions_in_bounds
        if found:
            gate_reason = SemanticGateReason.RESIDUAL_PAIR_DETERMINISTIC_MISMATCH
        elif not descriptions_in_bounds:
            gate_reason = SemanticGateReason.RESIDUAL_DESCRIPTION_OUT_OF_BOUNDS
        else:
            gate_reason = SemanticGateReason.RESIDUAL_PAIR_TEXT_ONLY

        # The two NORMALISED descriptions are stored, because they are exactly
        # what a model call would be given and nothing more. The raw supplier
        # text stays in the purgeable snapshot.
        result.residual_comparison = {
            "bill_line_index": bill_index,
            "po_line_index": po_index,
            "bill_line_description": bill_text,
            "po_line_description": po_text,
            "quantity_delta": _delta(bill_line.quantity, po_line.quantity),
            "unit_price_delta": _delta(bill_line.unit_amount, po_line.unit_amount),
            "line_amount_delta": _delta(bill_line.line_amount, po_line.line_amount),
            "tax_amount_delta": _delta(bill_line.tax_amount, po_line.tax_amount),
            "bill_tax_type": normalise_tax_type(bill_line.tax_type) or None,
            "po_tax_type": normalise_tax_type(po_line.tax_type) or None,
            "account_codes": vars(evidence),
            "exception_codes": [c.value for c in found],
            "descriptions_in_bounds": descriptions_in_bounds,
            "all_checks_passed": residual_clean,
        }

    return _finalise(result, exceptions, semantic_review_enabled, gate_reason, residual_clean)


def _delta(left: Decimal | None, right: Decimal | None) -> str | None:
    """Exact decimal strings, never floats."""
    if left is None or right is None:
        return None
    return str(left - right)


def _finalise(
    result: ReconciliationResult,
    exceptions: list[ExceptionItem],
    semantic_review_enabled: bool,
    gate_reason: SemanticGateReason = SemanticGateReason.NO_RESIDUAL_PAIR,
    residual_clean: bool = False,
) -> ReconciliationResult:
    """9. the gate, and 10. outcome, routing, reasons, stage."""
    result.exceptions = exceptions
    codes = set(result.exception_codes)

    # No exceptions at all means a clean bill. Recorded, completed, and then
    # silence: no Slack, no model call, not even a note. A triage queue that
    # fills with confirmations of things that were already fine is a queue
    # people stop reading.
    if not codes:
        result.outcome = Outcome.MATCHED
        result.semantic_permitted = False
        result.semantic_gate_reason = SemanticGateReason.OUTCOME_NOT_REVIEW_REQUIRED
        result.semantic_stage_status = SemanticStageStatus.NOT_REQUIRED
        return result

    result.outcome = Outcome.REVIEW_REQUIRED

    # Any blocking code means no model call, whatever the residual comparison
    # found. A recommendation about wording is irrelevant when the numbers, the
    # supplier or the document itself already disagree.
    blocking = codes & SEMANTIC_BLOCKING_CODES
    if blocking:
        result.semantic_permitted = False
        result.semantic_gate_reason = (
            gate_reason
            if gate_reason
            in (
                SemanticGateReason.RESIDUAL_PAIR_DETERMINISTIC_MISMATCH,
                SemanticGateReason.MULTIPLE_RESIDUAL_CANDIDATES,
            )
            else SemanticGateReason.HEADER_OR_PAIRED_LINE_EXCEPTION
        )
    elif residual_clean:
        # I26: exactly one residual pair, every deterministic check passed,
        # wording is the sole unresolved difference.
        result.semantic_permitted = True
        result.semantic_gate_reason = SemanticGateReason.RESIDUAL_PAIR_TEXT_ONLY
    else:
        result.semantic_permitted = False
        result.semantic_gate_reason = gate_reason

    # Configuration is NOT part of the deterministic gate. The gate
    # result and the then-current flag are combined once, and the captured flag
    # is recorded, so a later configuration change cannot reinterpret this run.
    if not result.semantic_permitted:
        result.semantic_stage_status = SemanticStageStatus.NOT_REQUIRED
    elif semantic_review_enabled:
        result.semantic_stage_status = SemanticStageStatus.PENDING
    else:
        result.semantic_stage_status = SemanticStageStatus.DISABLED

    result.triage_destination = route(codes)

    reasons = [HumanReviewReason.DETERMINISTIC_EXCEPTION]
    if ExceptionCode.AMBIGUOUS_MULTIPLE_CANDIDATES in codes:
        reasons.append(HumanReviewReason.AMBIGUOUS_MULTIPLE_CANDIDATES)
    if result.semantic_stage_status is SemanticStageStatus.DISABLED:
        reasons.append(HumanReviewReason.SEMANTIC_REVIEW_DISABLED)
    result.human_review_reasons = reasons

    return result
