"""Decimal-safe JSON parsing.

Monetary and quantity values must never pass through a binary floating-point
representation **at any point, including during JSON deserialisation**.

    REQUIRED:  parse the raw response body with a parser configured to produce
               Decimal for every JSON number.

    FORBIDDEN: letting a default parser produce a float and then converting
               with Decimal(str(value)). By that point the value has already
               been through a binary float and the representation error is
               baked in.

This is why the client reads the response as text and parses it here, rather
than calling a convenience `.json()` accessor whose parser cannot be
configured.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any


def loads(body: str) -> Any:
    """Parse a Xero response body with every JSON number as a Decimal."""
    return json.loads(body, parse_float=Decimal, parse_int=Decimal)


class DecimalEncoder(json.JSONEncoder):
    """Serialise Decimal as an exact string, never as a float.

    A variance written to the database or shown on a Slack card must carry the
    exact decimal string. json.dumps would otherwise raise on Decimal, and
    coercing to float would reintroduce the error this module exists to avoid.
    """

    def default(self, o: Any) -> Any:
        if isinstance(o, Decimal):
            return str(o)
        return super().default(o)


def dumps(value: Any) -> str:
    return json.dumps(value, cls=DecimalEncoder, separators=(",", ":"), sort_keys=True)


def account_codes_from_accounts_response(payload: dict) -> frozenset[str]:
    """The bounded account-code projection used as the reference set.

    AccountCode is read as a STRING with leading zeroes intact. A JSON number
    is refused rather than coerced: this layer guarantees a code arrives as a
    string, and `policy_service/domain/reconciliation.py` decides what is done with it.
    """
    codes: set[str] = set()
    for account in payload.get("Accounts", []):
        raw = account.get("Code")
        if raw is None:
            continue
        if not isinstance(raw, str):
            raise TypeError(
                f"AccountCode arrived as {type(raw).__name__}, expected str. "
                f"Coercing it would destroy leading zeroes."
            )
        cleaned = raw.strip()
        if cleaned:
            codes.add(cleaned)
    return frozenset(codes)


def account_reference_hash(codes: frozenset[str]) -> str:
    """Reproduces which validated reference set was used, after the purgeable
    snapshot holding the full response has gone."""
    import hashlib

    joined = "|".join(sorted(codes))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()
