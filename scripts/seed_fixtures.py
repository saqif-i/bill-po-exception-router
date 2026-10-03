#!/usr/bin/env python3
"""Load the fixture allow-list from the captured fixtures in tests/fixtures/.

I14: only records on the ACTIVE allow-list may become runs, and a bill is
reconciled only against a purchase order that is on it too. This adds one
ACTIVE row per captured bill and purchase order. Rows that already exist, by
Xero id or by fixture reference, are left alone, so it is safe to re-run.

Applied as bpr_owner: the runtime role cannot write seed_fixtures.

    python scripts/seed_fixtures.py
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import uuid
from datetime import UTC, datetime

import psycopg

FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "tests" / "fixtures"

# (directory, response key, Xero id field, human reference field, resource type)
SOURCES = (
    ("bills", "Invoices", "InvoiceID", "InvoiceNumber", "INVOICE"),
    (
        "purchase_orders",
        "PurchaseOrders",
        "PurchaseOrderID",
        "PurchaseOrderNumber",
        "PURCHASE_ORDER",
    ),
)


def fixture_rows(seed_run_id: uuid.UUID, now: datetime) -> list[tuple]:
    rows = []
    for directory, key, id_field, ref_field, resource_type in SOURCES:
        for path in sorted((FIXTURES / directory).glob("*.json")):
            record = json.loads(path.read_text())[key][0]
            rows.append(
                (
                    uuid.uuid4(),
                    seed_run_id,
                    path.stem,
                    f"BPR-SEED-{path.stem}",
                    resource_type,
                    record[id_field],
                    record[ref_field],
                    now,
                )
            )
    return rows


def main() -> int:
    owner_url = os.environ.get("BPR_OWNER_DATABASE_URL")
    if not owner_url:
        print("BPR_OWNER_DATABASE_URL is required", file=sys.stderr)
        return 2

    rows = fixture_rows(uuid.uuid4(), datetime.now(UTC))
    added: list[str] = []
    with psycopg.connect(owner_url) as conn, conn.cursor() as cur:
        for row in rows:
            cur.execute(
                """
                INSERT INTO seed_fixtures (
                    fixture_id, seed_run_id, fixture_name, fixture_reference,
                    xero_resource_type, xero_resource_id, human_reference,
                    expected_scenario, disposition, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'DEMO', 'REUSED', %s)
                ON CONFLICT DO NOTHING
                RETURNING fixture_reference
                """,
                row,
            )
            inserted = cur.fetchone()
            if inserted:
                added.append(inserted[0])
        conn.commit()

    print(f"{len(added)} added, {len(rows) - len(added)} already present")
    for reference in added:
        print(f"  {reference}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
