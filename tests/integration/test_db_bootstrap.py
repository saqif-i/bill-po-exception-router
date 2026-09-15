"""Migration and role boundary, against a real PostgreSQL.

Skipped unless BPR_OWNER_DATABASE_URL is set, so `make test` stays green on a
machine with no database. CI sets it and runs these.
"""

from __future__ import annotations

import os

import pytest

psycopg = pytest.importorskip("psycopg")

OWNER_URL = os.environ.get("BPR_OWNER_DATABASE_URL")
APP_URL = os.environ.get("BPR_DATABASE_URL")

pytestmark = pytest.mark.skipif(not OWNER_URL or not APP_URL, reason="no database configured")


def _fails(url: str, sql: str, params: tuple = ()) -> bool:
    """True when the statement is refused. Refusal is the assertion."""
    try:
        with psycopg.connect(url) as conn, conn.cursor() as cur:
            cur.execute(sql, params)
        return False
    except Exception:
        return True


def test_migrations_are_recorded() -> None:
    with psycopg.connect(OWNER_URL) as conn, conn.cursor() as cur:
        cur.execute("SELECT filename FROM schema_migrations")
        applied = {row[0] for row in cur.fetchall()}
    assert "001_core_schema.sql" in applied


def test_runtime_role_cannot_mutate_the_event_log() -> None:
    """integration_events is append-only by privilege, not only by trigger."""
    assert _fails(APP_URL, "UPDATE integration_events SET event_status = 'x'")
    assert _fails(APP_URL, "DELETE FROM integration_events")


def test_runtime_role_cannot_delete_purgeable_payloads() -> None:
    """I20: retention is not the request path's job."""
    assert _fails(APP_URL, "DELETE FROM integration_event_payloads")
    assert _fails(APP_URL, "DELETE FROM run_snapshots")


def test_runtime_role_cannot_write_the_fixture_allow_list() -> None:
    """I14: the service reads the allow-list. Only demo_seed writes it."""
    assert _fails(
        APP_URL,
        "INSERT INTO seed_fixtures (fixture_id, seed_run_id, fixture_name, "
        "fixture_reference, xero_resource_type, xero_resource_id, human_reference, "
        "expected_scenario, disposition, fixture_status, created_at) VALUES "
        "(gen_random_uuid(), gen_random_uuid(), 'x', 'BPR-SEED-999', 'INVOICE', "
        "gen_random_uuid(), 'INV-999', 'MATCHED', 'CREATED', 'ACTIVE', now())",
    )


def test_runtime_role_cannot_run_ddl() -> None:
    assert _fails(APP_URL, "CREATE TABLE should_not_exist (id int)")


def test_a_run_cannot_reference_an_inactive_fixture() -> None:
    """I14, enforced by trigger as well as by the composite foreign key."""
    with psycopg.connect(OWNER_URL) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO seed_fixtures (fixture_id, seed_run_id, fixture_name, "
            "fixture_reference, xero_resource_type, xero_resource_id, human_reference, "
            "expected_scenario, disposition, fixture_status, created_at) VALUES "
            "(gen_random_uuid(), gen_random_uuid(), 'retired', 'BPR-SEED-RETIRED', "
            "'INVOICE', gen_random_uuid(), 'INV-R', 'MATCHED', 'CREATED', 'RETIRED', now()) "
            "ON CONFLICT DO NOTHING RETURNING fixture_id, xero_resource_id"
        )
        row = cur.fetchone()
        if row is None:
            cur.execute(
                "SELECT fixture_id, xero_resource_id FROM seed_fixtures "
                "WHERE fixture_reference = 'BPR-SEED-RETIRED'"
            )
            row = cur.fetchone()
        conn.commit()

    assert _fails(
        OWNER_URL,
        "INSERT INTO runs (run_id, correlation_id, seed_fixture_id, xero_invoice_id, "
        "xero_invoice_number, ingestion_version_key, bill_hash) VALUES "
        "(gen_random_uuid(), gen_random_uuid(), %s, %s, 'INV-R', 'v1', 'hash')",
        (row[0], row[1]),
    )


def test_outcome_cannot_be_set_while_reconciling() -> None:
    """I28: a run is never observable with an outcome set while still RECONCILING."""
    assert _fails(
        OWNER_URL,
        "INSERT INTO runs (run_id, correlation_id, seed_fixture_id, xero_invoice_id, "
        "xero_invoice_number, ingestion_version_key, bill_hash, workflow_status, "
        "reconciliation_outcome) VALUES (gen_random_uuid(), gen_random_uuid(), "
        "gen_random_uuid(), gen_random_uuid(), 'X', 'v1', 'h', 'RECONCILING', 'MATCHED')",
    )
