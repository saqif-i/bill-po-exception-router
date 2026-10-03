"""The Xero read client: not-found, retries and token replacement. No network."""

from __future__ import annotations

import httpx
import pytest

from policy_service.integrations.xero_client import BASE_URL, XeroApiError, XeroReadClient
from policy_service.integrations.xero_errors import ErrorClass
from policy_service.integrations.xero_transport import RetrySchedule

BILL_ID = "521a0543-5885-4749-8ba3-40bf8a94a5bf"
ORG = '{"Organisations": [{"Name": "Demo Company"}]}'


class Scripted:
    """Answers each request with the next item: a Response, or an exception."""

    def __init__(self, *answers) -> None:
        self.answers = list(answers)
        self.requests = 0
        self.paths: list[str] = []
        self.sleeps: list[float] = []
        self.invalidations = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        self.paths.append(request.url.raw_path.decode())
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def client(self, schedule: RetrySchedule | None = None, refresh: bool = True):
        return XeroReadClient(
            token_provider=lambda: "test-token-not-real",
            invalidate_token=(lambda: setattr(self, "invalidations", self.invalidations + 1))
            if refresh
            else None,
            schedule=schedule or RetrySchedule(),
            sleep=self.sleeps.append,
            _client=httpx.Client(base_url=BASE_URL, transport=httpx.MockTransport(self)),
        )


def _client(response: httpx.Response) -> XeroReadClient:
    return Scripted(response).client(refresh=False)


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


def test_a_bill_is_fetched_by_id() -> None:
    body = '{"Invoices": [{"InvoiceID": "abc", "Total": 10.50}]}'
    assert _client(httpx.Response(200, text=body)).get_invoice(BILL_ID)["InvoiceID"] == "abc"


def test_a_missing_bill_is_none() -> None:
    assert _client(httpx.Response(404)).get_invoice(BILL_ID) is None


def test_fetching_a_bill_does_not_hide_an_outage() -> None:
    with pytest.raises(XeroApiError):
        _client(httpx.Response(503)).get_invoice(BILL_ID)


# --- retries (I13) ----------------------------------------------------------
def test_a_transient_failure_is_retried() -> None:
    script = Scripted(httpx.Response(503), httpx.Response(503), httpx.Response(200, text=ORG))
    assert script.client().get_organisation()["Organisations"][0]["Name"] == "Demo Company"
    assert script.requests == 3
    assert len(script.sleeps) == 2


def test_a_retry_after_is_waited_in_full() -> None:
    script = Scripted(
        httpx.Response(429, headers={"Retry-After": "2"}), httpx.Response(200, text=ORG)
    )
    script.client().get_organisation()
    assert script.sleeps and script.sleeps[0] >= 1.9


def test_a_retry_after_longer_than_the_worker_ceiling_releases_the_work() -> None:
    """Never shortened, and never blocked on: the call raises to be retried later."""
    script = Scripted(httpx.Response(429, headers={"Retry-After": "120"}))
    with pytest.raises(XeroApiError) as exc:
        script.client().get_organisation()
    assert exc.value.status == 429
    assert script.requests == 1
    assert script.sleeps == []


def test_total_sleep_in_one_call_stays_under_the_worker_ceiling() -> None:
    script = Scripted(httpx.Response(429, headers={"Retry-After": "2"}))
    schedule = RetrySchedule(worker_max_sleep_seconds=3)
    with pytest.raises(XeroApiError):
        script.client(schedule).get_organisation()
    assert script.requests == 2
    assert sum(script.sleeps) <= 3


def test_retries_stop_at_the_attempt_budget() -> None:
    script = Scripted(httpx.Response(503))
    with pytest.raises(XeroApiError):
        script.client(RetrySchedule(max_attempts=3)).get_organisation()
    assert script.requests == 3


def test_a_permanent_failure_is_not_retried() -> None:
    script = Scripted(httpx.Response(400))
    with pytest.raises(XeroApiError):
        script.client().get_organisation()
    assert script.requests == 1


def test_a_network_error_is_retried() -> None:
    script = Scripted(httpx.ConnectError("refused"), httpx.Response(200, text=ORG))
    script.client().get_organisation()
    assert script.requests == 2


def test_a_401_replaces_the_cached_token_once() -> None:
    script = Scripted(httpx.Response(401), httpx.Response(200, text=ORG))
    script.client().get_organisation()
    assert script.invalidations == 1
    assert script.requests == 2


def test_a_second_401_is_a_real_credential_failure() -> None:
    script = Scripted(httpx.Response(401))
    with pytest.raises(XeroApiError) as exc:
        script.client().get_organisation()
    assert exc.value.error_class is ErrorClass.AUTH_FAILURE
    assert script.invalidations == 1
    assert script.requests == 2


def test_a_purchase_order_number_containing_a_slash_is_encoded() -> None:
    """Unencoded, "PO-1/2" was two path segments, the allow-list refused it,
    and the run failed the same way on every retry."""
    body = '{"PurchaseOrders": [{"PurchaseOrderNumber": "PO-1/2"}]}'
    script = Scripted(httpx.Response(200, text=body))
    order = script.client().find_purchase_order("PO-1/2")
    assert order["PurchaseOrderNumber"] == "PO-1/2"
    assert script.paths[0].startswith("/api.xro/2.0/PurchaseOrders/PO-1%2F2")
