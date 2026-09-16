"""The runtime read client (Volume 04 section 9.11).

Read-only. Every call goes through the transport allow-list, which contains no
write method (ADR-006). Every response body is read as TEXT and parsed with the
Decimal-safe parser, never with a convenience .json() accessor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from policy_service.integrations import xero_parsing
from policy_service.integrations.xero_errors import ErrorClass, classify, safe_headers
from policy_service.integrations.xero_transport import RetrySchedule, assert_allowed

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
    connect_timeout: float = 10.0
    read_timeout: float = 30.0
    schedule: RetrySchedule = field(default_factory=RetrySchedule)
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

    def _get(self, operation: str, path: str, params: dict | None = None) -> Any:
        assert_allowed(operation, "GET", path)
        response = self._client.get(
            path,
            params=params,
            headers={
                "Authorization": f"Bearer {self.token_provider()}",
                "Accept": "application/json",
            },
        )
        if response.status_code >= 400:
            raise XeroApiError(
                response.status_code,
                classify(response.status_code, response.text),
                safe_headers(dict(response.headers)),
            )
        # Read as TEXT and parse ourselves. A .json() accessor cannot be
        # configured to produce Decimal, and by the time it returns a float the
        # representation error is already baked in.
        return xero_parsing.loads(response.text)

    def get_organisation(self) -> dict:
        return self._get("get_organisation", "/api.xro/2.0/Organisation")

    def list_invoices(self, *, modified_since: str | None, max_records: int) -> dict:
        """Draft ACCPAY bills modified since the poll watermark."""
        params = {
            "where": 'Type=="ACCPAY"&&Status=="DRAFT"',
            "order": "UpdatedDateUTC ASC",
            "page": 1,
            "pageSize": max_records,
            # A four-decimal supplier price must not be rounded before it is
            # compared. Subject to verifying the parameter against source S08.
            "unitdp": 4,
        }
        headers_extra = {"If-Modified-Since": modified_since} if modified_since else {}
        assert_allowed("list_invoices", "GET", "/api.xro/2.0/Invoices")
        response = self._client.get(
            "/api.xro/2.0/Invoices",
            params=params,
            headers={
                "Authorization": f"Bearer {self.token_provider()}",
                "Accept": "application/json",
                **headers_extra,
            },
        )
        if response.status_code >= 400:
            raise XeroApiError(
                response.status_code,
                classify(response.status_code, response.text),
                safe_headers(dict(response.headers)),
            )
        return xero_parsing.loads(response.text)

    def get_purchase_order(self, number: str) -> dict:
        return self._get(
            "get_purchase_order",
            f"/api.xro/2.0/PurchaseOrders/{number}",
            {"unitdp": 4},
        )

    def list_accounts(self) -> dict:
        return self._get("list_accounts", "/api.xro/2.0/Accounts")

    def chart_of_accounts(self) -> frozenset[str]:
        return xero_parsing.account_codes_from_accounts_response(self.list_accounts())
