"""Pure functions. No I/O, no configuration, no state.

Normalisation must be used IDENTICALLY for
pairing and for anything sent to a model provider. If the two diverged, the
model could be shown text that never had a chance to pair, and the gate's
guarantee would be meaningless. That is why these live in one module and are
tested against a fixed input and expected-output table.

It must never attempt stemming, synonym expansion, unit conversion or spelling
correction. Those are judgement calls, and judgement calls are what the human
is for.
"""

from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation

# Bounded project format for an account code.
ACCOUNT_CODE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,31}$")

_WHITESPACE = re.compile(r"\s+")

# A purchase-order reference inside a bill's single free-text field.
#
# Xero surfaces only ONE free-text field on an ACCPAY invoice: the UI labels it
# "Reference" and the API returns it as InvoiceNumber. The API `Reference` field
# is ACCREC only. There is therefore no separate place to record which purchase
# order a bill relates to.
#
# So the one field carries both, on a documented convention:
#
#     INV-1001 PO-1001
#
# A production system would use a tracking category, a custom field, or Xero's
# native copy-to-bill linkage. This is recorded as a limitation rather than
# presented as a design.
PO_REFERENCE_PATTERN = re.compile(r"\bPO-[A-Za-z0-9._/-]+", re.IGNORECASE)
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def trim(value: str | None) -> str:
    """Remove surrounding whitespace. The only canonicalisation an account
    code ever receives."""
    return (value or "").strip()


def collapse_whitespace(value: str | None) -> str:
    return _WHITESPACE.sub(" ", trim(value))


def normalise_strict(value: str | None) -> str:
    """Pairing tier 2: trim, collapse internal whitespace, casefold.

    Unicode is NFKC-normalised first so that visually identical text from two
    different sources compares equal.
    """
    text = unicodedata.normalize("NFKC", value or "")
    return collapse_whitespace(text).casefold()


def normalise_loose(value: str | None) -> str:
    """Pairing tier 3: tier 2 plus removal of punctuation and runs of
    non-alphanumeric characters.

    "Water, bottled (case of 24)" and "Water bottled - case of 24" collapse to
    the same string. "24x 500ml water" and "water, 500 ml x24" do NOT, because
    token order is preserved: reordering would be a judgement call.
    """
    return _NON_ALNUM.sub(" ", normalise_strict(value)).strip()


def canonical_account_code(value: str | None) -> str | None:
    """Surrounding whitespace only.

    Internal whitespace, punctuation and character case are unchanged, and
    LEADING ZEROES ARE PRESERVED. An account code is an identifier, not a
    number: "0010" and "10" are different accounts.
    """
    if value is None:
        return None
    trimmed = trim(value)
    return trimmed or None


def account_code_format_valid(canonical: str | None) -> bool:
    return canonical is not None and bool(ACCOUNT_CODE_PATTERN.match(canonical))


def normalise_tax_type(value: str | None) -> str:
    """Trimmed upper-case. Tax is COMPARED, never computed, derived or
    validated. This project reasons about no tax treatment."""
    return trim(value).upper()


def normalise_currency(value: str | None) -> str:
    """Upper-case ASCII."""
    return trim(value).upper()


def to_decimal(value: object) -> Decimal | None:
    """Decimal-safe parsing (see policy_service/integrations/xero_parsing.py).

    A float is rejected rather than converted, because 0.1 + 0.2 is not 0.3 and
    a financial comparison must not inherit that. Xero returns JSON numbers;
    the transport layer preserves them as strings for exactly this reason.
    """
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return None
    if isinstance(value, float):
        raise TypeError(
            "refusing to build a Decimal from a float. Parse the JSON with "
            "parse_float=Decimal or keep the raw string."
        )
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def within_tolerance(left: Decimal | None, right: Decimal | None, tolerance: Decimal) -> bool:
    """True when the two values agree within an ABSOLUTE tolerance.

    A missing value on either side is never 'within tolerance': absence is not
    agreement, and treating it as such would silently pass a malformed line.
    """
    if left is None or right is None:
        return False
    return abs(left - right) <= tolerance


def split_bill_reference(raw: str | None) -> tuple[str, str | None]:
    """Split a bill's single free-text field into (invoice number, PO reference).

    Returns the PO reference as None when the field names no purchase order,
    which is exactly the NO_PO_REFERENCE case rather than an error.

        "INV-1001 PO-1001" -> ("INV-1001", "PO-1001")
        "INV-1007"         -> ("INV-1007", None)
        ""                 -> ("", None)
    """
    text = collapse_whitespace(raw)
    if not text:
        return "", None

    match = PO_REFERENCE_PATTERN.search(text)
    if match is None:
        return text, None

    po_reference = match.group(0)
    # Strip any separator left behind where the reference was removed, so
    # "INV-1 / PO-1" yields "INV-1" rather than "INV-1 /".
    invoice_number = collapse_whitespace(text[: match.start()] + " " + text[match.end() :]).strip(
        " /|,;-"
    )
    return invoice_number, po_reference
