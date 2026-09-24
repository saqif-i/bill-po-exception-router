"""Which team owns this exception.

Routing is DETERMINISTIC. A model recommendation, where present, is displayed
as context and is never a routing input. Two runs with identical exceptions and
opposite model recommendations route identically.

The mapping below encodes an assumption. `docs/business-requirements.md` open
question 2 records that a real team would have to confirm which discrepancies
finance owns and which procurement owns.
"""

from __future__ import annotations

from collections.abc import Iterable

from policy_service.domain.enums import ExceptionCode, TriageDestination

# Procurement owns the supplier relationship and the purchase order.
_PROCUREMENT = frozenset(
    {
        ExceptionCode.NO_PO_REFERENCE,
        ExceptionCode.PO_NOT_FOUND,
        ExceptionCode.PO_NOT_ALLOW_LISTED,
        ExceptionCode.PO_STATUS_NOT_ELIGIBLE,
        ExceptionCode.SUPPLIER_MISMATCH,
        ExceptionCode.QUANTITY_VARIANCE,
        ExceptionCode.UNIT_PRICE_VARIANCE,
        ExceptionCode.LINE_AMOUNT_VARIANCE,
        ExceptionCode.RESIDUAL_QUANTITY_VARIANCE,
        ExceptionCode.RESIDUAL_UNIT_PRICE_VARIANCE,
        ExceptionCode.RESIDUAL_LINE_AMOUNT_VARIANCE,
    }
)

# Finance owns the chart of accounts and tax treatment.
_FINANCE = frozenset(
    {
        ExceptionCode.CURRENCY_MISMATCH,
        ExceptionCode.TAX_TYPE_MISMATCH,
        ExceptionCode.MISSING_TAX_TYPE,
        ExceptionCode.LINE_TAX_VARIANCE,
        ExceptionCode.MISSING_BILL_ACCOUNT_CODE,
        ExceptionCode.MISSING_PO_ACCOUNT_CODE,
        ExceptionCode.INVALID_BILL_ACCOUNT_CODE,
        ExceptionCode.INVALID_PO_ACCOUNT_CODE,
        ExceptionCode.UNKNOWN_BILL_ACCOUNT_CODE,
        ExceptionCode.UNKNOWN_PO_ACCOUNT_CODE,
        ExceptionCode.ACCOUNT_CODE_MISMATCH,
        ExceptionCode.RESIDUAL_TAX_TYPE_MISMATCH,
        ExceptionCode.RESIDUAL_MISSING_TAX_TYPE,
        ExceptionCode.RESIDUAL_LINE_TAX_VARIANCE,
        ExceptionCode.RESIDUAL_MISSING_BILL_ACCOUNT_CODE,
        ExceptionCode.RESIDUAL_MISSING_PO_ACCOUNT_CODE,
        ExceptionCode.RESIDUAL_INVALID_BILL_ACCOUNT_CODE,
        ExceptionCode.RESIDUAL_INVALID_PO_ACCOUNT_CODE,
        ExceptionCode.RESIDUAL_UNKNOWN_BILL_ACCOUNT_CODE,
        ExceptionCode.RESIDUAL_UNKNOWN_PO_ACCOUNT_CODE,
        ExceptionCode.RESIDUAL_ACCOUNT_CODE_MISMATCH,
    }
)

_DUPLICATE = frozenset(
    {
        ExceptionCode.DUPLICATE_INVOICE_NUMBER,
        ExceptionCode.DUPLICATE_BUSINESS_KEY,
    }
)

# Header aggregates. These are CONSEQUENCES of a line-level difference, not
# causes: a tax-rate change moves the tax, which moves the total. Routing on a
# consequence sends the case to whoever owns the symptom rather than the
# problem, so they are consulted only when nothing more specific is present.
#
# Alone, a total that disagrees with no line disagreeing is a procurement
# question; a tax total that disagrees with no line tax disagreeing is a finance
# one.
_AGGREGATE: dict[ExceptionCode, TriageDestination] = {
    ExceptionCode.TOTAL_VARIANCE: TriageDestination.PROCUREMENT,
    ExceptionCode.TAX_VARIANCE: TriageDestination.FINANCE,
}

# Everything else: line-count and wording questions an AP officer resolves.
_AP_REVIEW = frozenset(
    {
        ExceptionCode.LINE_COUNT_MISMATCH,
        ExceptionCode.UNPAIRED_LINE,
        ExceptionCode.AMBIGUOUS_MULTIPLE_CANDIDATES,
    }
)

# A run usually carries several exceptions, so precedence must be explicit and
# stable. A suspected duplicate outranks everything: if the bill should not
# exist at all, a price discussion with the supplier is premature.
_PRECEDENCE: tuple[tuple[TriageDestination, frozenset[ExceptionCode]], ...] = (
    (TriageDestination.DUPLICATE_REVIEW, _DUPLICATE),
    (TriageDestination.PROCUREMENT, _PROCUREMENT),
    (TriageDestination.FINANCE, _FINANCE),
    (TriageDestination.AP_REVIEW, _AP_REVIEW),
)


def route(codes: Iterable[ExceptionCode]) -> TriageDestination:
    """The single destination for a set of exception codes.

    Specific causes are consulted first. Header aggregates are consulted only
    when no specific cause is present, because they are consequences: a tax-rate
    difference on one line moves the line tax, the tax total and the invoice
    total, and routing on the total would send a tax question to procurement.
    """
    present = set(codes)
    for destination, owned in _PRECEDENCE:
        if present & owned:
            return destination
    for code, destination in _AGGREGATE.items():
        if code in present:
            return destination
    return TriageDestination.AP_REVIEW


def coverage_gaps() -> set[ExceptionCode]:
    """Every exception code must have an owner. A test asserts this is empty,
    so adding a code without routing it fails the build rather than silently
    defaulting to AP_REVIEW."""
    owned = _PROCUREMENT | _FINANCE | _DUPLICATE | _AP_REVIEW | set(_AGGREGATE)
    return set(ExceptionCode) - owned
