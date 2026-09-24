"""Bearer authentication with a constant-time comparison.

`==` on a secret leaks length and prefix through timing,
so the comparison helper is used everywhere and a unit test asserts that `==`
does not appear in the authentication path.
"""

from __future__ import annotations

import hmac

from fastapi import Request

from policy_service.api.errors import ServiceError
from policy_service.config import get_settings

# The fixed server-side principal for the internal bearer token.
POLICY_SCHEDULER = "policy-scheduler"


def constant_time_equals(supplied: str, expected: str) -> bool:
    return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


def require_internal_bearer(request: Request) -> str:
    """Return the authenticated principal, or raise. The caller never supplies it."""
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise ServiceError(code="UNAUTHENTICATED", status_code=401)
    if not constant_time_equals(token, get_settings().internal_bearer_token):
        raise ServiceError(code="UNAUTHENTICATED", status_code=401)
    return POLICY_SCHEDULER
