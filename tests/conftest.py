"""Shared test configuration.

It must not import application modules at collection time. An autouse fixture
that imported `policy_service.config` would make **every** test in the
repository error whenever that module failed to import, including tests that
never touch settings.
"""

from __future__ import annotations

import os

import pytest

# Integration tests create fixtures, runs and decisions. Left pointed at the
# application database they pollute the demo: you run `make gate`, then query
# for your nine bills and find forty rows from a fake model.
#
# So they run against a SEPARATE database when one is configured. This is done
# at module level rather than in a fixture because pytest imports conftest
# before collecting the test modules, and those read the variable at import.
_TEST_DB = os.environ.get("BPR_TEST_DATABASE_URL")
if _TEST_DB:
    os.environ["BPR_OWNER_DATABASE_URL"] = _TEST_DB

# Defaults for unit tests, which never reach a database or a provider.
TEST_ENV = {
    "BPR_DATABASE_URL": "postgresql://bpr_app:not-a-real-password@localhost:5433/bpr",
    "INTERNAL_BEARER_TOKEN": "test-token-for-unit-tests-only-0000",
    "XERO_WRITE_MODE": "disabled",
}


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch):
    """Environment defaults, and a settings-cache reset when there is one.

    `setdefault`, not `setenv`: overwriting would replace a real CI or local
    database URL with a dummy password, and the integration tests would then
    fail to connect while looking like a configuration problem.

    The settings cache is cleared only if the module imports. Importing it
    unconditionally would couple every test in the repository to that module.
    """
    for key, value in TEST_ENV.items():
        if not os.environ.get(key):
            monkeypatch.setenv(key, value)

    _clear_settings_cache()
    yield
    _clear_settings_cache()


def _clear_settings_cache() -> None:
    try:
        from policy_service.config import get_settings
    except ImportError:
        return  # the settings module is unavailable
    get_settings.cache_clear()


@pytest.fixture
def owner_url() -> str | None:
    return os.environ.get("BPR_OWNER_DATABASE_URL")
