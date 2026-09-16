"""Live Xero checks.

I17: these run only against the Xero Demo Company, are explicitly marked, and
never run on public pull-request CI. `pytest.ini` sets `-m "not live"`, so a
bare `pytest` skips them.

    pytest -m live tests/live/test_xero_connection.py -v
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def client():
    import httpx

    from policy_service.config import get_settings
    from policy_service.integrations.xero_auth import (
        TokenCache,
        XeroCredentials,
        request_token,
    )
    from policy_service.integrations.xero_client import XeroReadClient

    settings = get_settings()
    if not settings.xero_client_id or not settings.xero_client_secret:
        pytest.skip("no Xero credentials configured")

    credentials = XeroCredentials(
        client_id=settings.xero_client_id,
        client_secret=settings.xero_client_secret,
        scopes=settings.xero_scopes,
    )
    cache = TokenCache()
    http = httpx.Client(timeout=30.0)
    return XeroReadClient(
        token_provider=lambda: cache.get(lambda: request_token(http, credentials))
    )


def test_the_connection_reaches_the_demo_company(client) -> None:
    """The first thing to verify, and the one that catches a wrong connection."""
    from policy_service.config import get_settings

    organisations = client.get_organisation()["Organisations"]
    assert organisations, "no organisation returned"
    name = organisations[0]["Name"]
    expected = get_settings().xero_expected_org_name
    assert expected.lower() in name.lower(), (
        f"connected to {name!r}, which is not the demo company. "
        f"Every later stage assumes synthetic data (I16)."
    )


def test_the_chart_of_accounts_is_readable(client) -> None:
    codes = client.chart_of_accounts()
    assert codes, "no account codes returned"
    assert all(isinstance(code, str) for code in codes)


def test_a_token_is_reused_rather_than_refetched(client) -> None:
    """Tokens last thirty minutes. Requesting one per call would be wasteful
    and would hit the rate limit on a large poll."""
    first = client.token_provider()
    second = client.token_provider()
    assert first == second
