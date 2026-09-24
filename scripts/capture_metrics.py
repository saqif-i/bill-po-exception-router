#!/usr/bin/env python3
"""The numbers for the README, read straight out of the database.

A result may be claimed only after it has been measured.
So this prints what is true of YOUR run, and nothing is hard-coded.

    python scripts/capture_metrics.py
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime

import psycopg

QUERIES = {
    "runs_by_outcome": """
        SELECT coalesce(reconciliation_outcome, 'PENDING') AS outcome, count(*)
          FROM runs GROUP BY 1 ORDER BY 1
    """,
    "exceptions_by_code": """
        SELECT exception_code, count(*) FROM exception_items
         GROUP BY 1 ORDER BY 2 DESC, 1
    """,
    "routing_by_destination": """
        SELECT triage_destination, count(*) FROM runs
         WHERE triage_destination IS NOT NULL GROUP BY 1 ORDER BY 1
    """,
    # The gate reason is recorded on EVERY run, so this answers "why was a model
    # consulted or not" without inferring it from the absence of a result.
    "semantic_gate_reasons": """
        SELECT semantic_gate_reason, count(*) FROM reconciliation_results
         GROUP BY 1 ORDER BY 2 DESC, 1
    """,
    "semantic_stage_status": """
        SELECT semantic_stage_status, count(*) FROM runs GROUP BY 1 ORDER BY 1
    """,
    "decisions_by_action": """
        SELECT action, count(*) FROM triage_decisions GROUP BY 1 ORDER BY 1
    """,
}

# The failed-runs view, as a query rather than a migration. v1 creates four
# migrations and the next numbers belong to deferred components; adding one here
# would break the promise that numbering is stable.
FAILED_RUNS = """
    SELECT r.xero_invoice_number, r.workflow_status, r.reconciliation_outcome,
           r.updated_at
      FROM runs r
     WHERE r.workflow_status IN ('INGESTED', 'RECONCILING', 'NOTIFY_PENDING',
                                 'ACTION_FAILED')
       AND r.updated_at < now() - interval '15 minutes'
     ORDER BY r.updated_at
"""


def main() -> int:
    url = os.environ.get("BPR_OWNER_DATABASE_URL")
    if not url:
        print("BPR_OWNER_DATABASE_URL is required", file=sys.stderr)
        return 2

    report: dict = {"captured_at": datetime.now(UTC).isoformat()}
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        for name, sql in QUERIES.items():
            cur.execute(sql)
            report[name] = {str(row[0]): row[1] for row in cur.fetchall()}
        cur.execute(FAILED_RUNS)
        report["stuck_runs"] = [
            {"invoice": r[0], "status": r[1], "outcome": r[2], "since": str(r[3])}
            for r in cur.fetchall()
        ]

    outcomes = report["runs_by_outcome"]
    total = sum(outcomes.values()) or 1
    reviewed = outcomes.get("REVIEW_REQUIRED", 0)
    invoked = sum(
        count
        for reason, count in report["semantic_gate_reasons"].items()
        if reason == "RESIDUAL_PAIR_TEXT_ONLY"
    )
    report["derived"] = {
        "total_runs": total,
        "matched_without_a_human": outcomes.get("MATCHED", 0),
        "reached_a_human": reviewed,
        "model_eligible": invoked,
        "model_eligible_rate": round(invoked / total, 3),
    }

    print(json.dumps(report, indent=2))
    if report["stuck_runs"]:
        print(
            f"\n{len(report['stuck_runs'])} run(s) stuck. See docs/runbook.md.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
