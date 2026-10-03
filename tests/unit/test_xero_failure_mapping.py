"""How a Xero failure becomes a response. A rejected request is permanent."""

from __future__ import annotations

import httpx
import pytest

from policy_service.api.runs import _xero_failure
from policy_service.integrations.xero_client import XeroApiError
from policy_service.integrations.xero_errors import ErrorClass
from policy_service.integrations.xero_transport import OperationNotAllowedError


@pytest.mark.parametrize(
    "exc",
    [
        XeroApiError(400, ErrorClass.PERMANENT, {}),
        OperationNotAllowedError("path '/x' does not match get_purchase_order"),
    ],
)
def test_a_rejected_request_is_a_422(exc) -> None:
    """It fails the same way on every retry, so retrying it is pointless."""
    failure = _xero_failure(exc)
    assert failure.status_code == 422
    assert failure.code == "XERO_REJECTED_REQUEST"


@pytest.mark.parametrize(
    "exc",
    [
        XeroApiError(503, ErrorClass.RETRYABLE, {}),
        XeroApiError(401, ErrorClass.AUTH_FAILURE, {}),
        httpx.ConnectError("refused"),
    ],
)
def test_anything_else_from_xero_is_a_retryable_503(exc) -> None:
    assert _xero_failure(exc).status_code == 503


def test_a_non_xero_error_is_not_mapped() -> None:
    assert _xero_failure(ValueError("boom")) is None
