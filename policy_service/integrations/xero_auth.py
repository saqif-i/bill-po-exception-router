"""Custom Connection token acquisition and caching.

OAuth 2.0 client credentials. No refresh token is issued; a new access token is
requested with only the client id and secret. Tokens last 30 minutes, so the
cache exists to avoid requesting one per API call, not to survive a restart.

No `xero-tenant-id` header is sent: a Custom Connection talks to exactly one
organisation.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import httpx

# The identity endpoint, not a credential.
IDENTITY_HOST = "https://identity.xero.com"
TOKEN_PATH = "/connect/token"  # noqa: S105 - a URL path, not a credential

# Refresh this far before actual expiry, so a token cannot expire mid-request.
_EXPIRY_MARGIN_SECONDS = 60


@dataclass(frozen=True)
class XeroCredentials:
    client_id: str
    client_secret: str
    scopes: str

    def __post_init__(self) -> None:
        # offline_access is for the authorisation-code flow. Sending it here is
        # a configuration error, not a harmless extra.
        if "offline_access" in self.scopes:
            raise ValueError("offline_access is not valid for the client credentials grant")


class TokenCache:
    """Thread-safe. The service is single-worker in v1, but a cache that is
    only correct under one worker is a trap for whoever adds the second."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at: float = 0.0

    def get(self, fetch) -> str:
        with self._lock:
            if self._token and time.monotonic() < self._expires_at:
                return self._token
            token, expires_in = fetch()
            self._token = token
            self._expires_at = time.monotonic() + max(0, expires_in - _EXPIRY_MARGIN_SECONDS)
            return token

    def invalidate(self) -> None:
        with self._lock:
            self._token = None
            self._expires_at = 0.0


def request_token(client: httpx.Client, credentials: XeroCredentials) -> tuple[str, int]:
    """Returns (access_token, expires_in_seconds).

    The secret is sent in the body over TLS. It never appears in a log line:
    the caller's logging hook strips Authorization, and this function logs
    nothing itself.
    """
    response = client.post(
        IDENTITY_HOST + TOKEN_PATH,
        data={
            "grant_type": "client_credentials",
            "client_id": credentials.client_id,
            "client_secret": credentials.client_secret,
            "scope": credentials.scopes,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    response.raise_for_status()
    payload = response.json()
    return payload["access_token"], int(payload.get("expires_in", 1800))
