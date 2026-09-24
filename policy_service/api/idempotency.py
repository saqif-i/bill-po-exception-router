"""The API-level idempotency contract (I10, I36).

Validation happens BEFORE any domain mutation. A malformed key must not be able
to leave a half-finished run behind.
"""

from __future__ import annotations

import hashlib
import re

from fastapi import Request

from policy_service.api.errors import ServiceError
from policy_service.integrations.xero_parsing import dumps

KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
MIN_KEY_LENGTH = 16
MAX_KEY_LENGTH = 128


def validate_key(request: Request) -> str:
    """422 INVALID_IDEMPOTENCY_KEY on missing, empty, malformed or over-length."""
    key = request.headers.get("idempotency-key", "")
    if (
        not key
        or len(key) < MIN_KEY_LENGTH
        or len(key) > MAX_KEY_LENGTH
        or not KEY_PATTERN.match(key)
    ):
        raise ServiceError(code="INVALID_IDEMPOTENCY_KEY", status_code=422)
    return key


def scope(principal: str, operation: str) -> str:
    """Always the authenticated caller plus a FIXED endpoint operation.

    Never a caller-selected table, predicate or SQL fragment: the scope is what
    stops one caller's key from colliding with another's, so it cannot be
    something the caller chooses.
    """
    return f"internal:{principal}:{operation}"


def canonical_hash(principal: str, operation: str, body: dict | None) -> str:
    """Deterministic JSON with sorted keys and explicit defaults.

    The caller identity comes from the authentication layer, never from the
    body. Trusting the body would let a caller replay another caller's key.
    """
    canonical = {
        "principal": principal,
        "operation": operation,
        "body": body if body is not None else {},
    }
    return hashlib.sha256(dumps(canonical).encode("utf-8")).hexdigest()


def key_fingerprint(key: str) -> str:
    """Logs carry a one-way fingerprint, never the raw key."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
