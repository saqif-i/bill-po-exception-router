"""Duplicate detection (I04).

Hashing and comparison. Never a model's judgement: a rule cannot be talked into
missing a duplicate, and a duplicate that slips through is a payment made twice.
"""

from __future__ import annotations

import hashlib
from decimal import ROUND_HALF_UP, Decimal

from policy_service.domain.normalisation import normalise_currency, trim


def _sha256(parts: list[str]) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def invoice_number_key(contact_id: str, invoice_number: str) -> str:
    """Constructed ONLY when the invoice number is non-empty.

    Eligibility guarantees that. Building a key over an empty string would make
    every bill without a number collide with every other, manufacturing false
    duplicates out of missing data. That is why MISSING_INVOICE_NUMBER is an
    eligibility failure rather than an exception.
    """
    cleaned = trim(invoice_number)
    if not cleaned:
        raise ValueError(
            "invoice_number_key requires a non-empty invoice number. "
            "Eligibility must reject the bill with MISSING_INVOICE_NUMBER first."
        )
    return _sha256([str(contact_id), cleaned.casefold()])


def business_key(
    contact_id: str,
    total: Decimal | None,
    currency: str | None,
    date_bucket: str,
    reference: str | None,
) -> str:
    """Catches the same bill re-sent under a new invoice number.

    The total is quantised to two places so that 100.5 and 100.50 produce the
    same key. The date bucket is the calendar day, supplied by the caller.
    """
    amount = (total or Decimal("0")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return _sha256(
        [
            str(contact_id),
            str(amount),
            normalise_currency(currency),
            date_bucket,
            trim(reference).casefold(),
        ]
    )
