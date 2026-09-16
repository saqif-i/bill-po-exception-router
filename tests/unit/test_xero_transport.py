"""The allow-list and the retry schedule, Volume 04 sections 9.3 to 9.5.

Pure functions, so every branch is tested without a network.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from policy_service.integrations.xero_errors import ErrorClass, classify, safe_headers
from policy_service.integrations.xero_transport import (
    ALLOWED_OPERATIONS,
    OperationNotAllowedError,
    RetrySchedule,
    assert_allowed,
    next_retry,
    no_write_methods_exist,
    parse_retry_after,
)

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
SCHEDULE = RetrySchedule()


# --- the allow-list --------------------------------------------------------
def test_v1_has_no_write_method_at_all() -> None:
    """ADR-006. The guarantee is provable by reading one short file."""
    assert no_write_methods_exist()
    assert all(m == "GET" for m, _ in ALLOWED_OPERATIONS.values())


def test_an_unknown_operation_is_refused() -> None:
    with pytest.raises(OperationNotAllowedError):
        assert_allowed("create_invoice", "POST", "/api.xro/2.0/Invoices")


def test_the_right_operation_with_the_wrong_method_is_refused() -> None:
    with pytest.raises(OperationNotAllowedError, match="refusing POST"):
        assert_allowed("list_invoices", "POST", "/api.xro/2.0/Invoices")


def test_a_history_note_path_is_not_on_the_list() -> None:
    """The one mutation the full design permits. This build does not have it."""
    with pytest.raises(OperationNotAllowedError):
        assert_allowed("get_purchase_order", "GET", "/api.xro/2.0/Invoices/abc/History")


# --- error classification --------------------------------------------------
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, ErrorClass.AUTH_FAILURE),
        (403, ErrorClass.AUTH_FAILURE),
        (429, ErrorClass.RETRYABLE),
        (500, ErrorClass.RETRYABLE),
        (503, ErrorClass.RETRYABLE),
        (408, ErrorClass.RETRYABLE),
        (400, ErrorClass.PERMANENT),
        (404, ErrorClass.PERMANENT),
    ],
)
def test_status_classification(status, expected) -> None:
    assert classify(status) is expected


def test_credentials_never_survive_header_capture() -> None:
    kept = safe_headers(
        {
            "Authorization": "Bearer secret-token-value",
            "X-API-Key": "another-secret",
            "Cookie": "session=abc",
            "Retry-After": "30",
            "X-MinLimit-Remaining": "58",
            "X-Invented-Header": "whatever",
        }
    )
    assert kept == {"retry-after": "30", "x-minlimit-remaining": "58"}


def test_an_unrecognised_header_is_dropped_not_truncated() -> None:
    """A provider cannot cause an unbounded value to be stored by inventing a
    header name."""
    assert safe_headers({"X-Something-New": "x" * 5000}) == {}


# --- Retry-After, invariant I13 -------------------------------------------
def test_a_positive_retry_after_is_honoured_exactly() -> None:
    decision = next_retry(
        attempt_count=1,
        error_class=ErrorClass.RETRYABLE,
        retry_after_header="30",
        schedule=SCHEDULE,
        now=NOW,
    )
    assert decision.should_retry
    assert decision.earliest_retry_at == NOW + timedelta(seconds=30)


def test_a_large_retry_after_is_honoured_not_distrusted() -> None:
    """I13: never shortened, and never reclassified as malformed merely for
    being large. There is no setting anywhere that shortens a provider wait."""
    decision = next_retry(
        attempt_count=1,
        error_class=ErrorClass.RETRYABLE,
        retry_after_header="3600",
        schedule=SCHEDULE,
        now=NOW,
    )
    assert decision.earliest_retry_at == NOW + timedelta(seconds=3600)
    assert decision.release_work is True  # released, not blocked in process
    assert decision.raise_alert is True  # a warning, which changes no timing


def test_the_worker_sleep_ceiling_never_changes_the_time() -> None:
    schedule = RetrySchedule(worker_max_sleep_seconds=5)
    decision = next_retry(
        attempt_count=1,
        error_class=ErrorClass.RETRYABLE,
        retry_after_header="900",
        schedule=schedule,
        now=NOW,
    )
    assert decision.earliest_retry_at == NOW + timedelta(seconds=900)
    assert decision.release_work is True


@pytest.mark.parametrize("header", [None, "", "soon", "0", "-5", "abc"])
def test_an_unusable_retry_after_falls_through_to_backoff(header) -> None:
    assert parse_retry_after(header) is None
    decision = next_retry(
        attempt_count=1,
        error_class=ErrorClass.RETRYABLE,
        retry_after_header=header,
        schedule=SCHEDULE,
        now=NOW,
    )
    assert decision.should_retry
    assert decision.reason == "backoff with full jitter"


def test_backoff_is_bounded_and_jittered() -> None:
    for attempt in range(1, 4):
        decision = next_retry(
            attempt_count=attempt,
            error_class=ErrorClass.RETRYABLE,
            retry_after_header=None,
            schedule=SCHEDULE,
            now=NOW,
        )
        delay = (decision.earliest_retry_at - NOW).total_seconds()
        assert 0 <= delay <= SCHEDULE.backoff_max_seconds


def test_an_auth_failure_is_never_retried() -> None:
    """Retrying a rejected credential burns the rate limit and delays the real
    diagnosis."""
    decision = next_retry(
        attempt_count=1,
        error_class=ErrorClass.AUTH_FAILURE,
        retry_after_header="30",
        schedule=SCHEDULE,
        now=NOW,
    )
    assert decision.should_retry is False


def test_a_permanent_error_is_never_retried() -> None:
    decision = next_retry(
        attempt_count=1,
        error_class=ErrorClass.PERMANENT,
        retry_after_header=None,
        schedule=SCHEDULE,
        now=NOW,
    )
    assert decision.should_retry is False


def test_the_attempt_budget_is_a_ceiling() -> None:
    decision = next_retry(
        attempt_count=SCHEDULE.max_attempts,
        error_class=ErrorClass.RETRYABLE,
        retry_after_header="10",
        schedule=SCHEDULE,
        now=NOW,
    )
    assert decision.should_retry is False
    assert "budget" in decision.reason
