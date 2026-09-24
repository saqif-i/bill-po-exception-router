"""Error classification.

A pure function from status and body to a class. Pure so it can be unit-tested
against fixtures without a network, and so the retry decision is never an
accident of where in the code the error happened to be caught.
"""

from __future__ import annotations

from enum import StrEnum


class ErrorClass(StrEnum):
    RETRYABLE = "RETRYABLE"
    PERMANENT = "PERMANENT"
    AUTH_FAILURE = "AUTH_FAILURE"


# Headers that may be recorded. Anything else is dropped before storage, so a
# provider cannot cause an unbounded or sensitive value to be persisted by
# inventing a header.
APPROVED_DIAGNOSTIC_HEADERS = frozenset(
    {
        "retry-after",
        "x-daylimit-remaining",
        "x-minlimit-remaining",
        "x-appminlimit-remaining",
        "xero-correlation-id",
    }
)

# Stripped before anything is logged or stored.
FORBIDDEN_HEADERS = frozenset({"authorization", "x-api-key", "cookie", "set-cookie"})

_MAX_HEADER_VALUE = 256


def classify(status: int, body: str = "") -> ErrorClass:
    """Map an HTTP status to a retry class.

    401 and 403 are AUTH_FAILURE rather than RETRYABLE: retrying a rejected
    credential just burns the rate limit and delays the real diagnosis.
    """
    if status in (401, 403):
        return ErrorClass.AUTH_FAILURE
    if status == 429:
        return ErrorClass.RETRYABLE
    if 500 <= status <= 599:
        return ErrorClass.RETRYABLE
    if status == 408:
        return ErrorClass.RETRYABLE
    return ErrorClass.PERMANENT


def safe_headers(headers: dict[str, str]) -> dict[str, str]:
    """Keep only approved diagnostic headers, normalised and bounded.

    An unrecognised key is rejected rather than truncated, so the stored set is
    exactly the documented one.
    """
    out: dict[str, str] = {}
    for name, value in headers.items():
        key = name.strip().lower()
        if key in FORBIDDEN_HEADERS:
            continue
        if key not in APPROVED_DIAGNOSTIC_HEADERS:
            continue
        out[key] = str(value)[:_MAX_HEADER_VALUE]
    return out
