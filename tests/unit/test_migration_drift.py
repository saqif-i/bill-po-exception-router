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


# --- triage actions and destinations: migration 005c --------------------------
TRIAGE_MIGRATION = MIGRATION.parent / "005c_escalation_and_information.sql"


def _listed(constraint: str, column: str) -> set[str]:
    """The quoted values in the last `<constraint> CHECK (... <column> ... IN (...))`."""
    import re

    sql = TRIAGE_MIGRATION.read_text()
    blocks = re.findall(
        rf"ADD CONSTRAINT {constraint} CHECK \((.*?)\)\s*(?:NOT VALID)?;", sql, re.S
    )
    assert blocks, f"{constraint} not found in {TRIAGE_MIGRATION.name}"
    assert column in blocks[-1]
    return set(re.findall(r"'([A-Z_]+)'", blocks[-1]))


def test_the_action_check_matches_the_triage_actions() -> None:
    from policy_service.domain.enums import TriageAction

    assert _listed("triage_action_valid", "action") == {a.value for a in TriageAction}


def test_both_destination_checks_match_the_destinations() -> None:
    from policy_service.domain.enums import TriageDestination

    expected = {d.value for d in TriageDestination}
    assert _listed("slack_destination_valid", "destination") == expected
    assert _listed("runs_triage_destination_valid", "triage_destination") == expected


def test_the_schema_and_the_service_agree_on_what_never_closes_a_run() -> None:
    from policy_service.domain.triage import NON_FINAL, NOTE_ACTIONS

    assert _listed("triage_handoff_is_not_final", "is_final") == {a.value for a in NON_FINAL}
    assert _listed("triage_note_required", "note") == {a.value for a in NOTE_ACTIONS}


def test_005c_leaves_the_transaction_to_its_caller() -> None:
    """The runner sends the file as one query, which PostgreSQL runs as one
    transaction; the migration test runs it inside its own and rolls it back.
    A COMMIT in the file would end that transaction and keep the test's undo."""
    import re

    sql = TRIAGE_MIGRATION.read_text()
    assert not re.search(r"^\s*(BEGIN|COMMIT|ROLLBACK)\b", sql, re.M | re.I)
