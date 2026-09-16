"""Decimal-safe parsing, Volume 04 section 9.8."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from policy_service.integrations.xero_parsing import (
    account_codes_from_accounts_response,
    account_reference_hash,
    dumps,
    loads,
)


def test_json_numbers_become_decimals_not_floats():
    payload = loads('{"UnitAmount": 0.1, "Quantity": 3}')
    assert isinstance(payload["UnitAmount"], Decimal)
    assert isinstance(payload["Quantity"], Decimal)


def test_the_representation_error_is_avoided_not_repaired():
    """The default parser bakes the error in before any conversion can help."""
    body = '{"a": 0.1, "b": 0.2}'
    naive = json.loads(body)
    assert naive["a"] + naive["b"] != 0.3  # the problem

    safe = loads(body)
    assert safe["a"] + safe["b"] == Decimal("0.3")  # avoided at parse time


def test_decimals_serialise_as_exact_strings():
    assert dumps({"delta": Decimal("0.1")}) == '{"delta":"0.1"}'


def test_account_codes_keep_leading_zeroes():
    payload = loads('{"Accounts": [{"Code": "0010"}, {"Code": " 0020 "}]}')
    assert account_codes_from_accounts_response(payload) == frozenset({"0010", "0020"})


def test_a_numeric_account_code_is_refused_not_coerced():
    """Coercing would destroy leading zeroes and silently change the identity
    of an account."""
    payload = loads('{"Accounts": [{"Code": 10}]}')
    with pytest.raises(TypeError, match="leading zeroes"):
        account_codes_from_accounts_response(payload)


def test_reference_hash_is_order_independent():
    a = account_reference_hash(frozenset({"0010", "0020"}))
    b = account_reference_hash(frozenset({"0020", "0010"}))
    assert a == b and len(a) == 64
