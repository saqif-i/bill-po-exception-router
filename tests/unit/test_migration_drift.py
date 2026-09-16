"""The database and the engine must not drift.

Migration 003's exception_code CHECK is generated from the enum. This test
regenerates the expected set and asserts the committed SQL still matches, so
adding a code to the enum without regenerating the migration fails the build.
"""

from __future__ import annotations

import pathlib

from policy_service.domain.enums import ExceptionCode

MIGRATION = pathlib.Path(__file__).resolve().parents[2] / "migrations" / "003_reconciliation.sql"


def test_every_exception_code_appears_in_the_check_constraint() -> None:
    sql = MIGRATION.read_text()
    missing = [c.value for c in ExceptionCode if f"'{c.value}'" not in sql]
    assert missing == [], f"migration 003 is missing {missing}; regenerate it"


def test_the_check_contains_no_code_the_engine_cannot_emit() -> None:
    import re

    sql = MIGRATION.read_text()
    block = re.search(r"exception_code_valid CHECK \(exception_code IN \((.*?)\)\)", sql, re.S)
    assert block
    in_sql = set(re.findall(r"'([A-Z_]+)'", block.group(1)))
    assert in_sql - {c.value for c in ExceptionCode} == set()
