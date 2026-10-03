"""The runtime read client.

Read-only. Every call goes through the transport allow-list, which contains no
write method (ADR-006). Every response body is read as TEXT and parsed with the
Decimal-safe parser, never with a convenience .json() accessor.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx

from policy_service.integrations import xero_parsing
from policy_service.integrations.xero_errors import ErrorClass, classify, safe_headers
from policy_service.integrations.xero_transport import RetrySchedule, assert_allowed, next_retry

BASE_URL = "https://api.xero.com"


class XeroApiError(RuntimeError):
    def __init__(self, status: int, error_class: ErrorClass, headers: dict[str, str]):
        super().__init__(f"Xero returned {status} ({error_class})")
        self.status = status
        self.error_class = error_class
        self.headers = headers


@dataclass
class XeroReadClient:
    """One organisation, read only.

    Timeouts are mandatory on every request: no unbounded call may exist.
    Certificate verification is always on and is not behind a flag.
    """

    token_provider: Any
    # Clears the cached token. Called once after a 401, so a token revoked or
    # expired early is replaced rather than retried until the budget runs out.
    invalidate_token: Callable[[], None] | None = None
    connect_timeout: float = 10.0
    read_timeout: float = 30.0
    schedule: RetrySchedule = field(default_factory=RetrySchedule)
    sleep: Callable[[float], None] = time.sleep
    # Closes whatever the token provider holds, such as its own HTTP client.
    on_close: Callable[[], None] | None = None
    _client: httpx.Client | None = None

    def __post_init__(self) -> None:
        if self._client is None:
            self._client = httpx.Client(
                base_url=BASE_URL,
                verify=True,  # never disabled, not behind a debug flag
                timeout=httpx.Timeout(
                    connect=self.connect_timeout,
                    read=self.read_timeout,
                    write=self.read_timeout,
                    pool=self.connect_timeout,
                ),
            )

    def close(self) -> None:
        self._client.close()
        if self.on_close is not None:
            self.on_close()

    def _get(
        self,
        operation: str,
        path: str,
        params: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """One allow-listed GET, retried as the schedule says (I13).

        A retryable status or a network error waits for `next_retry`, which never
        shortens a Retry-After. When the wait would take the total time spent
        sleeping in this call past `worker_max_sleep_seconds`, the call raises
        instead: the work is released to be retried later, not blocked on.
        """
        assert_allowed(operation, "GET", path)
        attempt = 0
        slept = 0.0
        token_replaced = False
        while True:
            attempt += 1
            try:
                response = self._client.get(
                    path,
                    params=params,
                    headers={
                        "Authorization": f"Bearer {self.token_provider()}",
                        "Accept": "application/json",
                        **(headers or {}),
                    },
                )
            except httpx.TransportError as exc:
                failure: Exception = exc
                error_class, retry_after = ErrorClass.RETRYABLE, None
            else:
                if response.status_code < 400:
                    # Read as TEXT and parse ourselves. A .json() accessor cannot be
                    # configured to produce Decimal, and by the time it returns a
                    # float the representation error is already baked in.
                    return xero_parsing.loads(response.text)
                diagnostic = safe_headers(dict(response.headers))
                error_class = classify(response.status_code, response.text)
                retry_after = diagnostic.get("retry-after")
                failure = XeroApiError(response.status_code, error_class, diagnostic)
                if response.status_code == 401 and not token_replaced and self.invalidate_token:
                    self.invalidate_token()
                    token_replaced = True
                    continue

            decision = next_retry(
                attempt_count=attempt,
                error_class=error_class,
                retry_after_header=retry_after,
                schedule=self.schedule,
            )
            wait = 0.0
            if decision.earliest_retry_at is not None:
                wait = max(0.0, (decision.earliest_retry_at - datetime.now(UTC)).total_seconds())
            if (
                not decision.should_retry
                or decision.release_work
                or slept + wait > self.schedule.worker_max_sleep_seconds
            ):
                raise failure
            self.sleep(wait)
            slept += wait

    def get_organisation(self) -> dict:
        return self._get("get_organisation", "/api.xro/2.0/Organisation")

    def list_invoices(self, *, modified_since: str | None, max_records: int, page: int = 1) -> dict:
        """One page of draft ACCPAY bills modified since the poll watermark."""
        params = {
            "where": 'Type=="ACCPAY"&&Status=="DRAFT"',
            "order": "UpdatedDateUTC ASC",
            "page": page,
            "pageSize": max_records,
            # A four-decimal supplier price must not be rounded before it is
            # compared. Subject to verifying the parameter against source S08.
            "unitdp": 4,
        }
        headers = {"If-Modified-Since": modified_since} if modified_since else None
        return self._get("list_invoices", "/api.xro/2.0/Invoices", params, headers)

    def get_invoice(self, invoice_id: str) -> dict | None:
        """One bill by id, whatever its status, or None if Xero has no such bill.

        Fetched directly rather than found by scanning a page of drafts, which
        misses the bill once there are more drafts than fit on one page.
        """
        try:
            payload = self._get("get_invoice", f"/api.xro/2.0/Invoices/{invoice_id}", {"unitdp": 4})
        except XeroApiError as exc:
            if exc.status == 404:
                return None
            raise
        invoices = payload.get("Invoices", [])
        return invoices[0] if invoices else None

    def get_purchase_order(self, number: str) -> dict:
        # Encoded, because a reference may contain "/" (PO_REFERENCE_PATTERN
        # allows it). Unencoded, "PO-1/2" became two path segments, the
        # allow-list refused it, and the run failed the same way on every retry.
        return self._get(
            "get_purchase_order",
            f"/api.xro/2.0/PurchaseOrders/{quote(number, safe='')}",
            {"unitdp": 4},
        )

    def find_purchase_order(self, number: str) -> dict | None:
        """The purchase order, or None when Xero says it does not exist.

        Only a 404 means "not found". A rate limit, a server error or a rejected
        credential raises, so a transient failure cannot become a permanent
        PO_NOT_FOUND on the run.
        """
        try:
            payload = self.get_purchase_order(number)
        except XeroApiError as exc:
            if exc.status == 404:
                return None
            raise
        orders = payload.get("PurchaseOrders", [])
        return orders[0] if orders else None

    def list_accounts(self) -> dict:
        return self._get("list_accounts", "/api.xro/2.0/Accounts")

    def chart_of_accounts(self) -> frozenset[str]:
        return xero_parsing.account_codes_from_accounts_response(self.list_accounts())
