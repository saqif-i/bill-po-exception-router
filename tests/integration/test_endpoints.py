"""The endpoints' failure paths: what is reported, and whether the key can run again.

Drives the real endpoints, with the pool pointed at the test database and the
provider clients faked, so no Slack workspace or Xero connection is needed.
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


class ScriptedSlack:
    """Answers each post with the next outcome. Closed like the real client."""

    def __init__(self, *outcomes) -> None:
        self.outcomes = list(outcomes)
        self.posts = 0

    def post_card(self, **_kwargs):
        self.posts += 1
        return self.outcomes.pop(0)

    def close(self) -> None:
        pass


def _run_state(run_id):
    with psycopg.connect(OWNER_URL) as conn, conn.cursor() as cur:
        cur.execute("SELECT workflow_status FROM runs WHERE run_id = %s", (run_id,))
        status = cur.fetchone()[0]
        cur.execute("SELECT post_status FROM slack_notifications WHERE run_id = %s", (run_id,))
        post = cur.fetchone()
        return status, post[0] if post else None


@pytest.mark.parametrize(
    ("failure", "recorded_as"),
    [("channel_not_found", "FAILED"), ("TIMEOUT", "POSSIBLE_DUPLICATE")],
)
def test_a_card_that_was_not_posted_is_a_502_and_the_same_key_posts_it(
    client, monkeypatch, failure, recorded_as
):
    """Saved as a 200 success, a failed post was replayed on every retry, n8n
    routed the bill to Card Posted, and no card ever existed."""
    from policy_service.integrations import slack_client
    from policy_service.integrations.slack_client import PostOutcome
    from tests.integration.test_triage import _seed

    http, headers = client
    with psycopg.connect(OWNER_URL) as conn:
        run_id = _seed(conn)
        conn.commit()
    slack = ScriptedSlack(
        PostOutcome(False, error=failure, dispatch_unknown=failure == "TIMEOUT"),
        PostOutcome(True, message_ts="1700000000.000100", channel_id="C1"),
    )
    monkeypatch.setattr(slack_client, "SlackClient", lambda **_kwargs: slack)
    key = f"{run_id}-notify"

    first = http.post(f"/runs/{run_id}/notify", headers={**headers, "Idempotency-Key": key})
    after_failure = _run_state(run_id)
    recorded = _registry_row(key)
    second = http.post(f"/runs/{run_id}/notify", headers={**headers, "Idempotency-Key": key})
    after_retry = _run_state(run_id)
    again = http.post(
        f"/runs/{run_id}/notify", headers={**headers, "Idempotency-Key": f"{key}-again"}
    )

    assert first.status_code == 502
    assert first.json()["error"] == "CARD_NOT_POSTED"
    assert first.json()["status"] == recorded_as
    assert after_failure == ("REVIEW_READY", recorded_as)
    assert recorded == ("FAILED", "RETRYABLE", "CARD_NOT_POSTED")
    assert second.status_code == 200
    assert second.json()["posted"] is True
    assert "Idempotency-Replayed" not in second.headers
    assert after_retry == ("AWAITING_TRIAGE", "POSTED")
    assert again.status_code == 200 and again.json()["already"] is True
    assert slack.posts == 2


class RejectingXero:
    """A Xero client whose bill fetch is rejected as a bad request."""

    def get_invoice(self, _invoice_id):
        from policy_service.integrations.xero_client import XeroApiError
        from policy_service.integrations.xero_errors import ErrorClass

        raise XeroApiError(400, ErrorClass.PERMANENT, {})


def test_a_request_xero_rejects_is_a_422_and_is_not_retried(client, monkeypatch):
    """It was a 503 marked retryable, so it was retried for ever with the same
    result."""
    from policy_service.api import deps
    from tests.integration.test_triage import _seed

    http, headers = client
    with psycopg.connect(OWNER_URL) as conn:
        run_id = _seed(conn)
        conn.commit()
    monkeypatch.setattr(deps, "get_xero_client", lambda: RejectingXero())
    key = f"{run_id}-reconcile"

    first = http.post(f"/runs/{run_id}/reconcile", headers={**headers, "Idempotency-Key": key})
    recorded = _registry_row(key)
    second = http.post(f"/runs/{run_id}/reconcile", headers={**headers, "Idempotency-Key": key})

    assert first.status_code == 422
    assert first.json()["error"] == "XERO_REJECTED_REQUEST"
    assert recorded == ("FAILED", "PERMANENT", "RECONCILE_FAILED")
    assert second.status_code == 409
    assert second.json()["error"] == "IDEMPOTENCY_PERMANENT_FAILURE"
