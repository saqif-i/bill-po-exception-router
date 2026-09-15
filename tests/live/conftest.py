"""Live tests touch a real provider.

I17 requires the mechanism to exist, not merely that no live test happens to
run. That mechanism is `pytest_collection_modifyitems` below, NOT a module-level
`pytestmark`: a `pytestmark` in a conftest does **not** apply to the tests in
that directory, so an author who forgets the decorator would have their test run
on public CI.

    pytest                       -> live tests are deselected
    pytest -m live               -> live tests run
"""

from __future__ import annotations

import pathlib

import pytest


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark everything collected under tests/live/ as live, decorated or not.

    Enforcement belongs here rather than in each test, because the failure mode
    of the per-test approach is silent.
    """
    here = pathlib.Path(__file__).parent
    for item in items:
        if here in pathlib.Path(str(item.fspath)).parents:
            item.add_marker(pytest.mark.live)


@pytest.fixture(scope="session")
def demo_company_guard():
    """Refuse to run against anything that is not the demo company.

    A fixture, not import-time code: a provider call during collection would run
    even for a deselected test. It is requested explicitly by the tests that
    touch Xero.
    """
    from policy_service.config import get_settings

    settings = get_settings()
    if not settings.xero_client_id or not settings.xero_client_secret:
        pytest.skip("no Xero credentials configured")
    return settings.xero_expected_org_name
