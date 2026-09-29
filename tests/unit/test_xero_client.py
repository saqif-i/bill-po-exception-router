"""Only a 404 means a purchase order does not exist. No network."""

from __future__ import annotations

import httpx
import pytest

from policy_service.integrations.xero_client import BASE_URL, XeroApiError, XeroReadClient


def _client(response: httpx.Response) -> XeroReadClient:
    return XeroReadClient(
        token_provider=lambda: "test-token-not-real",
        _client=httpx.Client(
            base_url=BASE_URL, transport=httpx.MockTransport(lambda request: response)
        ),
    )


def test_a_404_is_not_found() -> None:
    assert _client(httpx.Response(404)).find_purchase_order("PO-1") is None


def test_an_empty_result_is_not_found() -> None:
    body = '{"PurchaseOrders": []}'
    assert _client(httpx.Response(200, text=body)).find_purchase_order("PO-1") is None


def test_a_found_order_is_returned() -> None:
    body = '{"PurchaseOrders": [{"PurchaseOrderNumber": "PO-1", "Total": 10.50}]}'
    order = _client(httpx.Response(200, text=body)).find_purchase_order("PO-1")
    assert order["PurchaseOrderNumber"] == "PO-1"


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
def test_any_other_failure_raises_rather_than_reading_as_not_found(status: int) -> None:
    """A rate limit or outage must not become a permanent PO_NOT_FOUND."""
    with pytest.raises(XeroApiError) as exc:
        _client(httpx.Response(status)).find_purchase_order("PO-1")
    assert exc.value.status == status
