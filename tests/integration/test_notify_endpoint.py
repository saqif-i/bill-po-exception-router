"""A refused notification is reported, and its key can run again.

Drives the real endpoint, with the pool pointed at the test database and the
domain call faked, so no Slack workspace is needed.
"""

from __future__ import annotations

import os
import uuid

import pytest

psycopg = pytest.importorskip("psycopg")

from fastapi.testclient import TestClient  # noqa: E402
from psycopg_pool import ConnectionPool  # noqa: E402

OWNER_URL = os.environ.get("BPR_OWNER_DATABASE_URL")
pytestmark = pytest.mark.skipif(not OWNER_URL, reason="no database configured")


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    from policy_service.api import runs
    from policy_service.config import get_settings
    from policy_service.main import app

    monkeypatch.setenv("SLACK_BOT_TOKEN", "test-slack-token-not-real")
    get_settings.cache_clear()
    pool = ConnectionPool(OWNER_URL, min_size=1, max_size=2, open=True)
    monkeypatch.setattr(runs, "get_pool", lambda: pool)
    token = get_settings().internal_bearer_token
    yield TestClient(app), {"Authorization": f"Bearer {token}"}
    pool.close()


def _registry_row(key: str):
    with psycopg.connect(OWNER_URL) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status, error_class, error_code FROM idempotency_registry "
            "WHERE idempotency_key = %s",
            (key,),
        )
        return cur.fetchone()


def test_a_refusal_is_a_409_and_the_same_key_runs_again(client, monkeypatch):
    """Recorded as success, a refusal was replayed for good, so re-running
    notify once the run was ready returned the old refusal and posted nothing."""
    from policy_service.domain import triage

    http, headers = client
    run_id = uuid.uuid4()
    key = f"{run_id}-notify"
    calls = []

    def refuse_then_post(conn, **kwargs):
        calls.append(kwargs["run_id"])
        if len(calls) == 1:
            raise triage.NotifyRefusedError("SEMANTIC_STAGE_NOT_TERMINAL_IN_PROGRESS")
        return {"posted": True, "status": "POSTED"}

    monkeypatch.setattr(triage, "notify", refuse_then_post)

    first = http.post(f"/runs/{run_id}/notify", headers={**headers, "Idempotency-Key": key})
    recorded = _registry_row(key)
    second = http.post(f"/runs/{run_id}/notify", headers={**headers, "Idempotency-Key": key})

    assert first.status_code == 409
    assert first.json()["error"] == "NOTIFY_REFUSED"
    assert first.json()["reason"] == "SEMANTIC_STAGE_NOT_TERMINAL_IN_PROGRESS"
    assert recorded == ("FAILED", "RETRYABLE", "NOTIFY_REFUSED")
    assert second.status_code == 200
    assert second.json()["posted"] is True
    assert "Idempotency-Replayed" not in second.headers
    assert len(calls) == 2
