"""Shared request dependencies.

The Xero client is built once and reused, so the token cache survives between
requests. Building it per request would request a token per request, which is
wasteful and would hit the rate limit on a large poll.
"""

from __future__ import annotations

from functools import lru_cache

import httpx

from policy_service.config import get_settings
from policy_service.integrations.xero_auth import (
    TokenCache,
    XeroCredentials,
    request_token,
)
from policy_service.integrations.xero_client import XeroReadClient
from policy_service.integrations.xero_transport import RetrySchedule


@lru_cache
def get_xero_client() -> XeroReadClient | None:
    """None when no credentials are configured, so the service still starts."""
    settings = get_settings()
    if not settings.xero_client_id or not settings.xero_client_secret:
        return None

    credentials = XeroCredentials(
        client_id=settings.xero_client_id,
        client_secret=settings.xero_client_secret,
        scopes=settings.xero_scopes,
    )
    cache = TokenCache()
    http = httpx.Client(timeout=settings.xero_read_timeout_seconds)
    return XeroReadClient(
        token_provider=lambda: cache.get(lambda: request_token(http, credentials)),
        invalidate_token=cache.invalidate,
        connect_timeout=settings.xero_connect_timeout_seconds,
        read_timeout=settings.xero_read_timeout_seconds,
        schedule=RetrySchedule(max_attempts=settings.xero_max_attempts),
        on_close=http.close,
    )


def close_xero_client() -> None:
    """Close the shared client and its token client, then forget them.

    Only a client that was actually built is closed: calling get_xero_client()
    here would build one at shutdown just to close it.
    """
    if get_xero_client.cache_info().currsize:
        client = get_xero_client()
        if client is not None:
            client.close()
    get_xero_client.cache_clear()
