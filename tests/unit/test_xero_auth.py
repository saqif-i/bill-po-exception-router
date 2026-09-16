"""Token acquisition and caching, Volume 04 section 9.2."""

from __future__ import annotations

import pytest

from policy_service.integrations.xero_auth import TokenCache, XeroCredentials


def test_offline_access_is_refused() -> None:
    """It belongs to the authorisation-code flow. Sending it with client
    credentials is a configuration error, not a harmless extra."""
    with pytest.raises(ValueError, match="offline_access"):
        XeroCredentials(
            client_id="id",
            client_secret="secret",
            scopes="accounting.settings.read offline_access",
        )


def test_a_cached_token_is_reused() -> None:
    cache = TokenCache()
    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        return f"token-{calls['n']}", 1800

    assert cache.get(fetch) == "token-1"
    assert cache.get(fetch) == "token-1"
    assert calls["n"] == 1


def test_a_short_lived_token_is_refetched() -> None:
    """The margin means a token is replaced before it can expire mid-request."""
    cache = TokenCache()
    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        return f"token-{calls['n']}", 1  # expires inside the safety margin

    assert cache.get(fetch) == "token-1"
    assert cache.get(fetch) == "token-2"


def test_invalidate_forces_a_refetch() -> None:
    cache = TokenCache()
    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        return f"token-{calls['n']}", 1800

    cache.get(fetch)
    cache.invalidate()
    assert cache.get(fetch) == "token-2"
