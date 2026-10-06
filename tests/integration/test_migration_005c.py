"""Migration 005c applies on top of the rows it changes the rules for.

Before 005c, Escalate and Request more information were final decisions with
no note. Those rows are history, and the migration must keep them as recorded.

bpr_owner cannot create a scratch database, so this test puts the test
database back in its 005b shape inside one transaction, records old-style rows,
runs the real 005c file, checks the result, and rolls all of it back.
"""

from __future__ import annotations

import os
import pathlib
import uuid

import pytest

psycopg = pytest.importorskip("psycopg")

from tests.integration.test_triage import _seed  # noqa: E402

OWNER_URL = os.environ.get("BPR_OWNER_DATABASE_URL")
TEST_URL = os.environ.get("BPR_TEST_DATABASE_URL")
# Only ever against the test database: this takes DDL locks, briefly, even
# though it commits nothing.
pytestmark = pytest.mark.skipif(not OWNER_URL or not TEST_URL, reason="no test database configured")

MIGRATION = (
    pathlib.Path(__file__).resolve().parents[2]
    / "migrations"
    / "005c_escalation_and_information.sql"
)

# 005c undone: the schema as 005b left it. The old rules come back NOT VALID
# because earlier test runs have left rows that only 005c allows. Those rows
# are rolled back to as they were with everything else.
UNDO_005C = """
DROP INDEX IF EXISTS uq_triage_one_escalation_per_run;
ALTER TABLE runs DROP CONSTRAINT IF EXISTS runs_triage_destination_valid;
ALTER TABLE slack_notifications DROP CONSTRAINT IF EXISTS slack_destination_valid;
ALTER TABLE slack_notifications ADD CONSTRAINT slack_destination_valid
    CHECK (destination IN ('FINANCE', 'PROCUREMENT', 'AP_REVIEW', 'DUPLICATE_REVIEW'))
    NOT VALID;
ALTER TABLE triage_decisions DROP CONSTRAINT IF EXISTS triage_note_required;
ALTER TABLE triage_decisions DROP CONSTRAINT IF EXISTS triage_note_bounded;
ALTER TABLE triage_decisions DROP COLUMN IF EXISTS note;
ALTER TABLE triage_decisions DROP CONSTRAINT IF EXISTS triage_action_valid;
ALTER TABLE triage_decisions ADD CONSTRAINT triage_action_valid CHECK (action IN (
    'MARK_REVIEWED', 'SEND_TO_FINANCE', 'SEND_TO_PROCUREMENT',
    'REQUEST_MORE_INFORMATION', 'ESCALATE', 'CLOSE_AS_DUPLICATE')) NOT VALID;
ALTER TABLE triage_decisions DROP CONSTRAINT IF EXISTS triage_handoff_is_not_final;
ALTER TABLE triage_decisions ADD CONSTRAINT triage_handoff_is_not_final CHECK (
    action NOT IN ('SEND_TO_FINANCE', 'SEND_TO_PROCUREMENT') OR NOT is_final) NOT VALID;
"""


def _decide(cur, run_id, action, *, is_final, note=None, before_005c=False):
    """A decision row. Before 005c there is no note column to name."""
    if before_005c:
        cur.execute(
            "INSERT INTO triage_decisions (decision_id, run_id, correlation_id, action, "
            "decided_by, shown_exception_codes, shown_destination, shown_gate_reason, "
            "slack_interaction_id, is_final) VALUES (%s, %s, %s, %s, 'U1', '{}', "
            "'PROCUREMENT', 'X', %s, %s)",
            (uuid.uuid4(), run_id, uuid.uuid4(), action, f"i-{uuid.uuid4()}", is_final),
        )
        return
    cur.execute(
        "INSERT INTO triage_decisions (decision_id, run_id, correlation_id, action, "
        "decided_by, shown_exception_codes, shown_destination, shown_gate_reason, "
        "slack_interaction_id, is_final, note) VALUES (%s, %s, %s, %s, 'U1', '{}', "
        "'PROCUREMENT', 'X', %s, %s, %s)",
        (uuid.uuid4(), run_id, uuid.uuid4(), action, f"i-{uuid.uuid4()}", is_final, note),
    )


def test_005c_applies_over_final_escalations_and_requests_and_keeps_them():
    from psycopg import errors

    with psycopg.connect(OWNER_URL) as conn:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT current_database()")
                if cur.fetchone()[0] != TEST_URL.rsplit("/", 1)[-1].split("?")[0]:
                    pytest.skip("not connected to the test database")
                cur.execute("SET LOCAL lock_timeout = '5s'")
                cur.execute(UNDO_005C)

                escalated, asked, fresh = _seed(conn), _seed(conn), _seed(conn)
                _decide(cur, escalated, "ESCALATE", is_final=True, before_005c=True)
                _decide(cur, asked, "REQUEST_MORE_INFORMATION", is_final=True, before_005c=True)

                cur.execute(MIGRATION.read_text())

                cur.execute(
                    "SELECT action, is_final, note FROM triage_decisions "
                    "WHERE run_id = ANY(%s) ORDER BY action",
                    ([escalated, asked],),
                )
                kept = cur.fetchall()
                cur.execute(
                    "SELECT conname, convalidated FROM pg_constraint "
                    "WHERE conrelid = ANY(ARRAY['triage_decisions'::regclass, "
                    "'slack_notifications'::regclass, 'runs'::regclass]) "
                    "AND conname = ANY(%s)",
                    (
                        [
                            "triage_handoff_is_not_final",
                            "triage_note_required",
                            "triage_note_bounded",
                            "triage_action_valid",
                            "slack_destination_valid",
                            "runs_triage_destination_valid",
                        ],
                    ),
                )
                validated = dict(cur.fetchall())

            refused = []
            for action, is_final, note in (
                ("ESCALATE", True, "A reason"),  # never final now
                ("ESCALATE", False, None),  # a reason is required
                ("REQUEST_MORE_INFORMATION", False, "x" * 501),  # bounded
            ):
                with pytest.raises(errors.CheckViolation), conn.transaction():
                    _decide(conn.cursor(), fresh, action, is_final=is_final, note=note)
                refused.append(action)
            with conn.transaction():
                _decide(conn.cursor(), fresh, "ESCALATE", is_final=False, note="A reason")
            with pytest.raises(errors.UniqueViolation), conn.transaction():
                _decide(conn.cursor(), fresh, "ESCALATE", is_final=False, note="Again")
        finally:
            # Nothing here may outlive the test: psycopg commits on a clean exit.
            conn.rollback()

    assert kept == [("ESCALATE", True, None), ("REQUEST_MORE_INFORMATION", True, None)]
    assert validated == {
        "triage_handoff_is_not_final": False,  # the historical rows would fail it
        "triage_note_required": False,
        "triage_note_bounded": True,
        "triage_action_valid": True,
        "slack_destination_valid": True,
        "runs_triage_destination_valid": True,
    }
    assert refused == ["ESCALATE", "ESCALATE", "REQUEST_MORE_INFORMATION"]
