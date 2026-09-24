"""The method-and-path allow-list, and the retry schedule.

Kept separate from the client so the allow-list is testable without
constructing a client, and so the fact that v1 has NO write method is provable
by reading one short file.

ADR-006: v1 performs no external mutation. There is no entry here that is not
a GET.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from policy_service.integrations.xero_errors import ErrorClass

# Every call the service is permitted to make. Adding a write here is a
# deliberate, reviewable act, not a side effect of adding a client method.
ALLOWED_OPERATIONS: dict[str, tuple[str, str]] = {
    "get_organisation": ("GET", r"^/api\.xro/2\.0/Organisation$"),
    "list_invoices": ("GET", r"^/api\.xro/2\.0/Invoices$"),
    "get_purchase_order": ("GET", r"^/api\.xro/2\.0/PurchaseOrders/[^/]+$"),
    "list_accounts": ("GET", r"^/api\.xro/2\.0/Accounts$"),
}


class OperationNotAllowedError(RuntimeError):
    """Raised when a call is not on the allow-list. Never caught and retried."""


def assert_allowed(operation: str, method: str, path: str) -> None:
    if operation not in ALLOWED_OPERATIONS:
        raise OperationNotAllowedError(f"{operation!r} is not an allow-listed operation")
    expected_method, pattern = ALLOWED_OPERATIONS[operation]
    if method.upper() != expected_method:
        raise OperationNotAllowedError(
            f"{operation} is {expected_method}, refusing {method.upper()}"
        )
    if not re.match(pattern, path):
        raise OperationNotAllowedError(f"path {path!r} does not match {operation}")


def no_write_methods_exist() -> bool:
    """v1 guarantee, asserted by a test."""
    return all(method == "GET" for method, _ in ALLOWED_OPERATIONS.values())


@dataclass(frozen=True)
class RetrySchedule:
    """Retry and backoff limits for Xero calls (I13)."""

    max_attempts: int = 4
    backoff_base_seconds: float = 1.0
    backoff_max_seconds: float = 60.0
    worker_max_sleep_seconds: float = 30.0
    retry_after_alert_threshold_seconds: float = 300.0


@dataclass(frozen=True)
class RetryDecision:
    should_retry: bool
    earliest_retry_at: datetime | None
    release_work: bool = False
    raise_alert: bool = False
    reason: str = ""


def parse_retry_after(value: str | None) -> float | None:
    """Return seconds if the header is syntactically valid and positive.

    Zero, negative and malformed values return None and fall through to
    backoff. A LARGE value returns as-is: it is honoured, not distrusted.
    """
    if value is None:
        return None
    try:
        seconds = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


def next_retry(
    *,
    attempt_count: int,
    error_class: ErrorClass,
    retry_after_header: str | None,
    schedule: RetrySchedule,
    now: datetime | None = None,
) -> RetryDecision:
    """Invariant I13.

    A syntactically valid, positive Retry-After is NEVER shortened, and is never
    reclassified as malformed merely for being large.

    There is no setting anywhere in this design that can shorten a
    provider-mandated wait. WORKER_MAX_SLEEP_SECONDS decides only whether a
    worker blocks in process or releases the work. The alert threshold raises a
    warning and never changes the time.
    """
    now = now or datetime.now(UTC)

    if error_class is ErrorClass.AUTH_FAILURE:
        return RetryDecision(False, None, reason="AUTH_FAILURE is never retried")
    if error_class is ErrorClass.PERMANENT:
        return RetryDecision(False, None, reason="PERMANENT is never retried")
    if attempt_count >= schedule.max_attempts:
        return RetryDecision(False, None, reason="attempt budget exhausted")

    seconds = parse_retry_after(retry_after_header)
    if seconds is not None:
        return RetryDecision(
            should_retry=True,
            earliest_retry_at=now + timedelta(seconds=seconds),
            # Release rather than block, without changing the time.
            release_work=seconds > schedule.worker_max_sleep_seconds,
            raise_alert=seconds > schedule.retry_after_alert_threshold_seconds,
            reason="provider Retry-After honoured exactly",
        )

    # No usable header: exponential backoff with full jitter.
    delay = min(
        schedule.backoff_base_seconds * (2 ** (attempt_count - 1)),
        schedule.backoff_max_seconds,
    )
    jittered = random.uniform(0, delay)  # noqa: S311 - jitter, not cryptography
    return RetryDecision(
        should_retry=True,
        earliest_retry_at=now + timedelta(seconds=jittered),
        release_work=jittered > schedule.worker_max_sleep_seconds,
        reason="backoff with full jitter",
    )
