"""Text handling.

Part 4 needs only the reference splitter, because a bill's purchase-order
reference lives inside a field Xero calls something else.

**Part 5 replaces this file with the complete version**, adding the pairing and
decimal helpers. The whole file is shown again there, so there is never a
fragment to place by hand.
"""

from __future__ import annotations

import re

_WHITESPACE = re.compile(r"\s+")

# A purchase-order reference inside a bill's single free-text field.
#
# Xero surfaces only ONE free-text field on an ACCPAY invoice: the UI labels it
# "Reference" and the API returns it as InvoiceNumber. The API `Reference` field
# is ACCREC only. There is therefore no separate place to record which purchase
# order a bill relates to, so the one field carries both:
#
#     INV-1001 PO-1001
#
# A production system would use a tracking category, a custom field, or Xero's
# native copy-to-bill linkage. This is recorded as a provider limitation in
# docs/limitations-and-roadmap.md rather than presented as a design.
PO_REFERENCE_PATTERN = re.compile(r"\bPO-[A-Za-z0-9._/-]+", re.IGNORECASE)


def trim(value: str | None) -> str:
    """Remove surrounding whitespace. The only canonicalisation an account
    code ever receives."""
    return (value or "").strip()


def collapse_whitespace(value: str | None) -> str:
    return _WHITESPACE.sub(" ", trim(value))


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
