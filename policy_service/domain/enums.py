"""The complete deterministic vocabulary.

Every code the reconciler can emit is named here. Nothing constructs a code as
a string literal elsewhere, so the set is closed and the Slack card, the
routing table and the tests all draw from the same source.
"""

from __future__ import annotations

from enum import StrEnum


class Outcome(StrEnum):
    """The three terminal reconciliation outcomes."""

    MATCHED = "MATCHED"
    UNPROCESSABLE = "UNPROCESSABLE"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


class UnprocessableReason(StrEnum):
    """Eligibility failures. These produce UNPROCESSABLE and never reach Slack.

    A record that failed contract validation has nothing a triage reviewer can
    act on and no purchase-order comparison to explain.
    """

    NOT_A_SUPPLIER_BILL = "NOT_A_SUPPLIER_BILL"
    BILL_STATUS_NOT_ELIGIBLE = "BILL_STATUS_NOT_ELIGIBLE"
    CONTRACT_INVALID = "CONTRACT_INVALID"
    MISSING_INVOICE_NUMBER = "MISSING_INVOICE_NUMBER"
    NO_INGESTION_VERSION = "NO_INGESTION_VERSION"


class ExceptionCode(StrEnum):
    """Exceptions. Every one produces REVIEW_REQUIRED and reaches a human."""

    # purchase-order resolution
    NO_PO_REFERENCE = "NO_PO_REFERENCE"
    PO_NOT_FOUND = "PO_NOT_FOUND"
    PO_NOT_ALLOW_LISTED = "PO_NOT_ALLOW_LISTED"
    PO_STATUS_NOT_ELIGIBLE = "PO_STATUS_NOT_ELIGIBLE"

    # header
    SUPPLIER_MISMATCH = "SUPPLIER_MISMATCH"
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    LINE_COUNT_MISMATCH = "LINE_COUNT_MISMATCH"
    UNPAIRED_LINE = "UNPAIRED_LINE"
    AMBIGUOUS_MULTIPLE_CANDIDATES = "AMBIGUOUS_MULTIPLE_CANDIDATES"

    # paired lines
    QUANTITY_VARIANCE = "QUANTITY_VARIANCE"
    UNIT_PRICE_VARIANCE = "UNIT_PRICE_VARIANCE"
    LINE_AMOUNT_VARIANCE = "LINE_AMOUNT_VARIANCE"
    TAX_TYPE_MISMATCH = "TAX_TYPE_MISMATCH"
    MISSING_TAX_TYPE = "MISSING_TAX_TYPE"
    LINE_TAX_VARIANCE = "LINE_TAX_VARIANCE"

    # header monetary
    TAX_VARIANCE = "TAX_VARIANCE"
    TOTAL_VARIANCE = "TOTAL_VARIANCE"

    # account codes, in validation order: missing, invalid, unknown, mismatch
    MISSING_BILL_ACCOUNT_CODE = "MISSING_BILL_ACCOUNT_CODE"
    MISSING_PO_ACCOUNT_CODE = "MISSING_PO_ACCOUNT_CODE"
    INVALID_BILL_ACCOUNT_CODE = "INVALID_BILL_ACCOUNT_CODE"
    INVALID_PO_ACCOUNT_CODE = "INVALID_PO_ACCOUNT_CODE"
    UNKNOWN_BILL_ACCOUNT_CODE = "UNKNOWN_BILL_ACCOUNT_CODE"
    UNKNOWN_PO_ACCOUNT_CODE = "UNKNOWN_PO_ACCOUNT_CODE"
    ACCOUNT_CODE_MISMATCH = "ACCOUNT_CODE_MISMATCH"

    # duplicates
    DUPLICATE_INVOICE_NUMBER = "DUPLICATE_INVOICE_NUMBER"
    DUPLICATE_BUSINESS_KEY = "DUPLICATE_BUSINESS_KEY"

    # residual pair (I26, I27). Same checks, separate codes, so a card can
    # say the wording differs AND the numbers differ.
    RESIDUAL_QUANTITY_VARIANCE = "RESIDUAL_QUANTITY_VARIANCE"
    RESIDUAL_UNIT_PRICE_VARIANCE = "RESIDUAL_UNIT_PRICE_VARIANCE"
    RESIDUAL_LINE_AMOUNT_VARIANCE = "RESIDUAL_LINE_AMOUNT_VARIANCE"
    RESIDUAL_TAX_TYPE_MISMATCH = "RESIDUAL_TAX_TYPE_MISMATCH"
    RESIDUAL_MISSING_TAX_TYPE = "RESIDUAL_MISSING_TAX_TYPE"
    RESIDUAL_LINE_TAX_VARIANCE = "RESIDUAL_LINE_TAX_VARIANCE"
    RESIDUAL_MISSING_BILL_ACCOUNT_CODE = "RESIDUAL_MISSING_BILL_ACCOUNT_CODE"
    RESIDUAL_MISSING_PO_ACCOUNT_CODE = "RESIDUAL_MISSING_PO_ACCOUNT_CODE"
    RESIDUAL_INVALID_BILL_ACCOUNT_CODE = "RESIDUAL_INVALID_BILL_ACCOUNT_CODE"
    RESIDUAL_INVALID_PO_ACCOUNT_CODE = "RESIDUAL_INVALID_PO_ACCOUNT_CODE"
    RESIDUAL_UNKNOWN_BILL_ACCOUNT_CODE = "RESIDUAL_UNKNOWN_BILL_ACCOUNT_CODE"
    RESIDUAL_UNKNOWN_PO_ACCOUNT_CODE = "RESIDUAL_UNKNOWN_PO_ACCOUNT_CODE"
    RESIDUAL_ACCOUNT_CODE_MISMATCH = "RESIDUAL_ACCOUNT_CODE_MISMATCH"


class HumanReviewReason(StrEnum):
    """Why a person is looking at this.

    Every REVIEW_REQUIRED run carries at least one. A successful model
    recommendation is itself a review reason, not an alternative to one.
    """

    DETERMINISTIC_EXCEPTION = "DETERMINISTIC_EXCEPTION"
    AMBIGUOUS_MULTIPLE_CANDIDATES = "AMBIGUOUS_MULTIPLE_CANDIDATES"
    SEMANTIC_RECOMMENDATION_AVAILABLE = "SEMANTIC_RECOMMENDATION_AVAILABLE"
    SEMANTIC_REVIEW_DISABLED = "SEMANTIC_REVIEW_DISABLED"
    SEMANTIC_OUTPUT_INVALID = "SEMANTIC_OUTPUT_INVALID"
    SEMANTIC_EVIDENCE_UNSUPPORTED = "SEMANTIC_EVIDENCE_UNSUPPORTED"
    SEMANTIC_LOW_CONFIDENCE = "SEMANTIC_LOW_CONFIDENCE"
    SEMANTIC_REFUSED = "SEMANTIC_REFUSED"
    SEMANTIC_TRUNCATED = "SEMANTIC_TRUNCATED"
    SEMANTIC_UNEXPECTED_STOP_REASON = "SEMANTIC_UNEXPECTED_STOP_REASON"
    SEMANTIC_TIMEOUT = "SEMANTIC_TIMEOUT"
    SEMANTIC_PROVIDER_UNAVAILABLE = "SEMANTIC_PROVIDER_UNAVAILABLE"


class TriageDestination(StrEnum):
    """Decided deterministically. A model recommendation is never an input."""

    FINANCE = "FINANCE"
    PROCUREMENT = "PROCUREMENT"
    AP_REVIEW = "AP_REVIEW"
    DUPLICATE_REVIEW = "DUPLICATE_REVIEW"


class TriageAction(StrEnum):
    """The Slack controls. I03: none is named Approve or Reject, because none
    of them is. These express operational triage, not financial approval."""

    MARK_REVIEWED = "MARK_REVIEWED"
    SEND_TO_FINANCE = "SEND_TO_FINANCE"
    SEND_TO_PROCUREMENT = "SEND_TO_PROCUREMENT"
    REQUEST_MORE_INFORMATION = "REQUEST_MORE_INFORMATION"
    ESCALATE = "ESCALATE"
    CLOSE_AS_DUPLICATE = "CLOSE_AS_DUPLICATE"


class SemanticGateReason(StrEnum):
    """Recorded on EVERY run, whether or not a model call was made, so the
    gate's behaviour is auditable rather than inferred from an absence."""

    RESIDUAL_PAIR_TEXT_ONLY = "RESIDUAL_PAIR_TEXT_ONLY"
    RESIDUAL_PAIR_DETERMINISTIC_MISMATCH = "RESIDUAL_PAIR_DETERMINISTIC_MISMATCH"
    RESIDUAL_DESCRIPTION_OUT_OF_BOUNDS = "RESIDUAL_DESCRIPTION_OUT_OF_BOUNDS"
    NO_RESIDUAL_PAIR = "NO_RESIDUAL_PAIR"
    MULTIPLE_RESIDUAL_CANDIDATES = "MULTIPLE_RESIDUAL_CANDIDATES"
    HEADER_OR_PAIRED_LINE_EXCEPTION = "HEADER_OR_PAIRED_LINE_EXCEPTION"
    OUTCOME_NOT_REVIEW_REQUIRED = "OUTCOME_NOT_REVIEW_REQUIRED"


class SemanticStageStatus(StrEnum):
    """Initialised once by the reconciliation transaction.

    `policy_service/domain/semantic.py` owns every transition after initialisation.
    """

    NOT_REQUIRED = "NOT_REQUIRED"
    DISABLED = "DISABLED"
    PENDING = "PENDING"


# Codes whose presence means a model must never be consulted, even if a clean
# single residual pair exists. A recommendation about wording is irrelevant
# when the supplier, the currency or the document itself already disagree.
SEMANTIC_BLOCKING_CODES: frozenset[ExceptionCode] = frozenset(
    {
        ExceptionCode.NO_PO_REFERENCE,
        ExceptionCode.PO_NOT_FOUND,
        ExceptionCode.PO_NOT_ALLOW_LISTED,
        ExceptionCode.PO_STATUS_NOT_ELIGIBLE,
        ExceptionCode.SUPPLIER_MISMATCH,
        ExceptionCode.CURRENCY_MISMATCH,
        ExceptionCode.AMBIGUOUS_MULTIPLE_CANDIDATES,
        ExceptionCode.QUANTITY_VARIANCE,
        ExceptionCode.UNIT_PRICE_VARIANCE,
        ExceptionCode.LINE_AMOUNT_VARIANCE,
        ExceptionCode.TAX_TYPE_MISMATCH,
        ExceptionCode.MISSING_TAX_TYPE,
        ExceptionCode.LINE_TAX_VARIANCE,
        ExceptionCode.TAX_VARIANCE,
        ExceptionCode.TOTAL_VARIANCE,
        ExceptionCode.MISSING_BILL_ACCOUNT_CODE,
        ExceptionCode.MISSING_PO_ACCOUNT_CODE,
        ExceptionCode.INVALID_BILL_ACCOUNT_CODE,
        ExceptionCode.INVALID_PO_ACCOUNT_CODE,
        ExceptionCode.UNKNOWN_BILL_ACCOUNT_CODE,
        ExceptionCode.UNKNOWN_PO_ACCOUNT_CODE,
        ExceptionCode.ACCOUNT_CODE_MISMATCH,
        ExceptionCode.DUPLICATE_INVOICE_NUMBER,
        ExceptionCode.DUPLICATE_BUSINESS_KEY,
        ExceptionCode.LINE_COUNT_MISMATCH,
    }
)
