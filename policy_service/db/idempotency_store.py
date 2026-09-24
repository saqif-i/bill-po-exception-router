"""The idempotency registry, against PostgreSQL.

`policy_service/api/idempotency.py` defines the wire contract; this implements it. The
behaviour table, which the tests follow row for row:

    existing record            incoming                response
    -------------------------  ----------------------  --------------------
    none                       any                     claim and execute
    in progress, same hash     duplicate in flight     202 IN_PROGRESS
    any state, different hash  key reuse               409 KEY_REUSED
    succeeded                  replay                  recorded body, 200
    failed, retryable          replay                  atomic reclamation
    failed, permanent          replay                  recorded error

The registry is claimed BEFORE the domain mutation, in the same transaction, so
a crash between claiming and working leaves a PROCESSING row with an expired
lease rather than a silent gap.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from psycopg import Connection

from policy_service.api.errors import ServiceError

LEASE_SECONDS = 300
RETENTION_DAYS = 90


@dataclass
class Claim:
    ledger_id: uuid.UUID
    replayed: bool = False
    result_status: int | None = None
    result_summary: dict | None = None


def claim(
    conn: Connection,
    *,
    scope: str,
    key: str,
    request_hash: str,
    correlation_id: uuid.UUID,
    owner_id: str,
) -> Claim:
    """Claim the key, or raise the correct refusal.

    The INSERT carries the whole claim, so two concurrent callers cannot both
    believe they won: the unique index decides, and the loser reads the winner's
    row rather than proceeding.
    """
    now = datetime.now(UTC)
    ledger_id = uuid.uuid4()

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO idempotency_registry (
                ledger_id, scope, idempotency_key, correlation_id, request_hash,
                status, owner_id, claim_generation, locked_at, lease_expires_at,
                attempt_count
            ) VALUES (%s, %s, %s, %s, %s, 'PROCESSING', %s, 0, %s, %s, 1)
            ON CONFLICT (scope, idempotency_key) DO NOTHING
            RETURNING ledger_id
            """,
            (
                ledger_id,
                scope,
                key,
                correlation_id,
                request_hash,
                owner_id,
                now,
                now + timedelta(seconds=LEASE_SECONDS),
            ),
        )
        row = cur.fetchone()
        if row is not None:
            return Claim(ledger_id=row[0])

        # Someone holds it. Read their row and answer from the table above.
        cur.execute(
            "SELECT ledger_id, status, error_class, request_hash, result_status, "
            "result_summary FROM idempotency_registry "
            "WHERE scope = %s AND idempotency_key = %s",
            (scope, key),
        )
        existing_id, status, error_class, existing_hash, result_status, summary = cur.fetchone()

        # Checked before anything else: a key reused with a different request is
        # refused whatever state it is in, and is never executed or overwritten.
        if existing_hash != request_hash:
            raise ServiceError(code="IDEMPOTENCY_KEY_REUSED", status_code=409)

        if status == "PROCESSING":
            raise ServiceError(code="IDEMPOTENCY_IN_PROGRESS", status_code=202)

        if status == "SUCCEEDED":
            return Claim(existing_id, True, result_status, summary)

        if status == "FAILED" and error_class == "RETRYABLE":
            cur.execute(
                """
                UPDATE idempotency_registry
                   SET status='PROCESSING', owner_id=%s, locked_at=%s,
                       lease_expires_at=%s, attempt_count=attempt_count+1,
                       error_class=NULL, error_code=NULL,
                       completed_at=NULL, retained_until=NULL, updated_at=now()
                 WHERE ledger_id=%s AND status='FAILED'
                RETURNING ledger_id
                """,
                (owner_id, now, now + timedelta(seconds=LEASE_SECONDS), existing_id),
            )
            if cur.fetchone() is None:
                raise ServiceError(code="IDEMPOTENCY_IN_PROGRESS", status_code=202)
            return Claim(existing_id)

        # FAILED and PERMANENT. Never re-executed.
        raise ServiceError(code="IDEMPOTENCY_PERMANENT_FAILURE", status_code=409)


def complete(conn: Connection, ledger_id: uuid.UUID, *, status_code: int, summary: dict) -> None:
    now = datetime.now(UTC)
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE idempotency_registry
               SET status='SUCCEEDED', owner_id=NULL, locked_at=NULL,
                   lease_expires_at=NULL, result_status=%s, result_summary=%s,
                   completed_at=%s, retained_until=%s, updated_at=now()
             WHERE ledger_id=%s
            """,
            (status_code, _json(summary), now, now + timedelta(days=RETENTION_DAYS), ledger_id),
        )


def fail(conn: Connection, ledger_id: uuid.UUID, *, error_class: str, error_code: str) -> None:
    now = datetime.now(UTC)
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE idempotency_registry
               SET status='FAILED', error_class=%s, error_code=%s,
                   owner_id=NULL, locked_at=NULL, lease_expires_at=NULL,
                   completed_at=%s, retained_until=%s, updated_at=now()
             WHERE ledger_id=%s
            """,
            (error_class, error_code, now, now + timedelta(days=RETENTION_DAYS), ledger_id),
        )


def _json(value: dict):
    import json as _stdlib_json

    from psycopg.types.json import Jsonb

    from policy_service.integrations.xero_parsing import dumps

    return Jsonb(_stdlib_json.loads(dumps(value)))
