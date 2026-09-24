"""Provider failures map to their own review reasons (I06). No network."""

from __future__ import annotations

import httpx
import pytest

from policy_service.integrations.claude_client import ClaudeClient
from policy_service.integrations.claude_contract import RejectionReason


def _client(handler) -> ClaudeClient:
    return ClaudeClient(
        api_key="test-key-not-real",
        model="claude-haiku-4-5-20251001",
        _client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def _review(client: ClaudeClient):
    return client.review(
        purchase_order_line_description="water, bottled, case of 24",
        bill_line_description="24x 500ml bottled water",
    )


def test_a_timeout_is_recorded_as_a_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    result = _review(_client(handler))
    assert result.rejection.reason is RejectionReason.TIMEOUT


def test_a_transport_failure_is_provider_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    result = _review(_client(handler))
    assert result.rejection.reason is RejectionReason.PROVIDER_UNAVAILABLE


@pytest.mark.parametrize("status", [429, 500, 529])
def test_a_provider_error_status_is_provider_unavailable(status: int) -> None:
    result = _review(_client(lambda request: httpx.Response(status)))
    assert result.rejection.reason is RejectionReason.PROVIDER_UNAVAILABLE
    assert str(status) in result.rejection.detail
