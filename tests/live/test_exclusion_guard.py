"""Proof that the live-test exclusion works, before there is anything live.

I17 requires the mechanism to exist, not merely that no live test happens to
run. This file is that proof, and it is deliberately **undecorated**: nothing
here says `@pytest.mark.live`.

If the exclusion works, the collection hook in conftest.py marks it anyway:

    pytest -q            -> deselected, and the count does not include it
    pytest -m live -q    -> 1 passed

If a bare `pytest` ever runs this, the mechanism is broken and a real live test
would reach public CI.

Part 4 replaces this with the first genuine live test.
"""

from __future__ import annotations


def test_the_exclusion_mechanism_is_in_place() -> None:
    assert True
