"""What the model may return, and what is done with it.

Volume 08 sections 9.8, 9.10 and 9.12. Pure functions, so every rejection path
is tested without an API key.

The contract has FOUR fields. Note what is absent: there is no field for an
amount, a quantity, a status, an action or an approval. The model has no channel
through which it could affect an outcome even if it tried, and no channel
through which text inside a supplier description could instruct it to.

That structural absence is what makes this safe. The other controls reduce how
often an attempt is made; this one removes the payoff.
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass
from enum import StrEnum

SCHEMA_DIR = pathlib.Path(__file__).resolve().parents[2] / "schemas"
MODEL_FACING_SCHEMA = json.loads(
    (SCHEMA_DIR / "semantic_review.model_facing.schema.json").read_text()
)
STRICT_SCHEMA = json.loads((SCHEMA_DIR / "semantic_review.strict.schema.json").read_text())

MAX_EXPLANATION = 400
MAX_EVIDENCE_SPANS = 4
MIN_EVIDENCE_SPANS = 1

# The explanation is for a reviewer deciding about wording. A sentence about
# price or approval would be the model reaching past its one question.
FORBIDDEN_IN_EXPLANATION = (
    "price",
    "prices",
    "amount",
    "amounts",
    "total",
    "totals",
    "quantity",
    "quantities",
    "tax",
    "approve",
    "approved",
    "approval",
    "reject",
    "rejected",
    "pay",
    "payment",
    "should be",
)


class Recommendation(StrEnum):
    LIKELY_EQUIVALENT = "LIKELY_EQUIVALENT"
    LIKELY_DIFFERENT = "LIKELY_DIFFERENT"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class EvidenceSource(StrEnum):
    PURCHASE_ORDER_LINE = "PURCHASE_ORDER_LINE"
    BILL_LINE = "BILL_LINE"


class RejectionReason(StrEnum):
    MALFORMED = "SEMANTIC_OUTPUT_INVALID"
    EVIDENCE_UNSUPPORTED = "SEMANTIC_EVIDENCE_UNSUPPORTED"
    LOW_CONFIDENCE = "SEMANTIC_LOW_CONFIDENCE"
    REFUSED = "SEMANTIC_REFUSED"
    TRUNCATED = "SEMANTIC_TRUNCATED"
    UNEXPECTED_STOP = "SEMANTIC_UNEXPECTED_STOP_REASON"


@dataclass(frozen=True)
class ValidatedRecommendation:
    recommendation: Recommendation
    confidence: float
    explanation: str
    evidence: tuple[dict[str, str], ...]


@dataclass(frozen=True)
class Rejection:
    reason: RejectionReason
    detail: str = ""


def build_request_payload(
    *, purchase_order_line_description: str, bill_line_description: str
) -> dict:
    """Exactly two Xero-derived business-data fields, and no more.

    Absent by design and asserted absent by the payload-minimisation test:
    supplier name, any GUID, invoice or purchase-order number, tenant id,
    correlation id, any monetary amount, quantity, account code, tax type, date,
    exception code or deterministic result.
    """
    return {
        "purchase_order_line_description": purchase_order_line_description,
        "bill_line_description": bill_line_description,
    }


def render_user_message(payload: dict) -> str:
    """Data is DELIMITED, not interpolated.

    The descriptions are serialised as JSON inside a named block, so the
    boundary between instructions and data is unambiguous and a description
    containing a quotation mark or a newline cannot break out of it.
    """
    return (
        "<candidate_pair>\n"
        + json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
        + "\n</candidate_pair>"
    )


def validate(
    raw: object,
    *,
    purchase_order_line_description: str,
    bill_line_description: str,
    min_confidence: float,
) -> ValidatedRecommendation | Rejection:
    """The strict server-side check. Bounds are enforced HERE, not requested."""
    if not isinstance(raw, dict):
        return Rejection(RejectionReason.MALFORMED, "not an object")

    required = set(STRICT_SCHEMA["required"])
    if set(raw) != required:
        return Rejection(
            RejectionReason.MALFORMED,
            f"keys {sorted(raw)} do not match {sorted(required)}",
        )

    try:
        recommendation = Recommendation(raw["recommendation"])
    except ValueError:
        return Rejection(RejectionReason.MALFORMED, "unknown recommendation value")

    confidence = raw["confidence"]
    if not isinstance(confidence, int | float) or isinstance(confidence, bool):
        return Rejection(RejectionReason.MALFORMED, "confidence is not a number")
    if not 0 <= confidence <= 1:
        return Rejection(RejectionReason.MALFORMED, "confidence outside 0 to 1")

    explanation = raw["explanation"]
    if not isinstance(explanation, str) or not explanation.strip():
        return Rejection(RejectionReason.MALFORMED, "explanation missing")
    if len(explanation) > MAX_EXPLANATION:
        return Rejection(RejectionReason.MALFORMED, "explanation too long")
    lowered = explanation.lower()
    if any(word in lowered for word in FORBIDDEN_IN_EXPLANATION):
        return Rejection(RejectionReason.MALFORMED, "explanation mentions a forbidden topic")

    evidence = raw["evidence"]
    if not isinstance(evidence, list):
        return Rejection(RejectionReason.MALFORMED, "evidence is not a list")
    if not MIN_EVIDENCE_SPANS <= len(evidence) <= MAX_EVIDENCE_SPANS:
        return Rejection(RejectionReason.MALFORMED, "wrong number of evidence spans")

    sources = {
        EvidenceSource.PURCHASE_ORDER_LINE: purchase_order_line_description,
        EvidenceSource.BILL_LINE: bill_line_description,
    }
    for span in evidence:
        if not isinstance(span, dict) or set(span) != {"source", "text"}:
            return Rejection(RejectionReason.MALFORMED, "malformed evidence span")
        try:
            source = EvidenceSource(span["source"])
        except ValueError:
            return Rejection(RejectionReason.MALFORMED, "unknown evidence source")
        text = span["text"]
        if not isinstance(text, str) or not text:
            return Rejection(RejectionReason.MALFORMED, "empty evidence span")
        # Verbatim substring, character for character. Injected instruction text
        # cannot be dressed up as evidence unless it literally appears in the
        # description, in which case it is shown to a person as the supplier's
        # own words.
        if text not in sources[source]:
            return Rejection(
                RejectionReason.EVIDENCE_UNSUPPORTED,
                f"span not found in {source.value}",
            )

    # Checked LAST, so a low-confidence answer that was also malformed is
    # reported as malformed. The more specific fault wins.
    if confidence < min_confidence:
        return Rejection(RejectionReason.LOW_CONFIDENCE, f"{confidence} below threshold")

    return ValidatedRecommendation(
        recommendation=recommendation,
        confidence=float(confidence),
        explanation=explanation,
        evidence=tuple(evidence),
    )
