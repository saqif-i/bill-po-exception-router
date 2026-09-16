"""The idempotency contract, Volume 02 section 9.9.

One test per row of the behaviour table.
"""

from __future__ import annotations

import os
import uuid

import pytest

psycopg = pytest.importorskip("psycopg")

from policy_service.api.errors import ServiceError  # noqa: E402
from policy_service.api.idempotency import (  # noqa: E402
    canonical_hash,
    key_fingerprint,
    scope,
)
from policy_service.db import idempotency_store  # noqa: E402

OWNER_URL = os.environ.get("BPR_OWNER_DATABASE_URL")
pytestmark = pytest.mark.skipif(not OWNER_URL, reason="no database configured")

PRINCIPAL = "policy-scheduler"


def _key() -> str:
    return f"test-{uuid.uuid4().hex}"


def _claim(conn, key, request_hash="a" * 64):
    return idempotency_store.claim(
        conn,
        scope=scope(PRINCIPAL, "runs-poll"),
        key=key,
        request_hash=request_hash,
        correlation_id=uuid.uuid4(),
        owner_id=PRINCIPAL,
    )


def test_a_fresh_key_is_claimed_and_executes():
    with psycopg.connect(OWNER_URL) as conn:
        claim = _claim(conn, _key())
        conn.commit()
    assert claim.replayed is False


def test_a_duplicate_in_flight_returns_202():
    key = _key()
    with psycopg.connect(OWNER_URL) as conn:
        _claim(conn, key)
        conn.commit()
        with pytest.raises(ServiceError) as exc:
            _claim(conn, key)
    assert exc.value.status_code == 202
    assert exc.value.code == "IDEMPOTENCY_IN_PROGRESS"


def test_key_reuse_with_a_different_request_is_refused():
    """409, never executed, never overwritten. Checked before state, so reuse
    is refused whatever the original is doing."""
    key = _key()
    with psycopg.connect(OWNER_URL) as conn:
        _claim(conn, key, request_hash="a" * 64)
        conn.commit()
        with pytest.raises(ServiceError) as exc:
            _claim(conn, key, request_hash="b" * 64)
    assert exc.value.status_code == 409
    assert exc.value.code == "IDEMPOTENCY_KEY_REUSED"


def test_a_succeeded_key_replays_the_recorded_body():
    key = _key()
    with psycopg.connect(OWNER_URL) as conn:
        first = _claim(conn, key)
        idempotency_store.complete(conn, first.ledger_id, status_code=200, summary={"ingested": 3})
        conn.commit()
        replay = _claim(conn, key)
    assert replay.replayed is True
    assert replay.result_summary == {"ingested": 3}
    assert replay.result_status == 200


def test_a_retryable_failure_is_reclaimed():
    key = _key()
    with psycopg.connect(OWNER_URL) as conn:
        first = _claim(conn, key)
        idempotency_store.fail(
            conn, first.ledger_id, error_class="RETRYABLE", error_code="POLL_FAILED"
        )
        conn.commit()
        again = _claim(conn, key)
        conn.commit()
    assert again.replayed is False
    assert again.ledger_id == first.ledger_id


def test_a_permanent_failure_is_never_re_executed():
    key = _key()
    with psycopg.connect(OWNER_URL) as conn:
        first = _claim(conn, key)
        idempotency_store.fail(
            conn, first.ledger_id, error_class="PERMANENT", error_code="BAD_REQUEST"
        )
        conn.commit()
        with pytest.raises(ServiceError) as exc:
            _claim(conn, key)
    assert exc.value.code == "IDEMPOTENCY_PERMANENT_FAILURE"


def test_the_scope_is_never_caller_selected():
    """It is always the authenticated principal plus a fixed operation."""
    assert scope("policy-scheduler", "runs-poll") == "internal:policy-scheduler:runs-poll"


def test_the_canonical_hash_ignores_a_body_claim_about_identity():
    """The caller identity comes from the authentication layer, never the body.
    Trusting the body would let a caller replay another caller's key."""
    honest = canonical_hash("policy-scheduler", "runs-poll", {})
    liar = canonical_hash("policy-scheduler", "runs-poll", {"principal": "someone-else"})
    assert honest != liar


def test_logs_carry_a_fingerprint_not_the_key():
    key = "a-very-secret-idempotency-key-value"
    assert key_fingerprint(key) != key
    assert len(key_fingerprint(key)) == 16
