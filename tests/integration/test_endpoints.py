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
    from policy_service.api import alerts, runs, slack
    from policy_service.config import get_settings
    from policy_service.main import app

    monkeypatch.setenv("SLACK_BOT_TOKEN", "test-slack-token-not-real")
    get_settings.cache_clear()
    pool = ConnectionPool(OWNER_URL, min_size=1, max_size=2, open=True)
    monkeypatch.setattr(runs, "get_pool", lambda: pool)
    monkeypatch.setattr(alerts, "get_pool", lambda: pool)
    monkeypatch.setattr(slack, "get_pool", lambda: pool)
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
        self.sent: list[dict] = []

    def post_card(self, **kwargs):
        self.posts += 1
        self.sent.append(kwargs)
        return self.outcomes.pop(0)

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


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


# --- alerts from the n8n error workflow --------------------------------------
def _alert(http, headers, key, **body):
    # A unique message by default: an identical alert posted in the last 30
    # minutes, by an earlier run of these tests too, would be suppressed.
    payload = {
        "workflow": "02-bill-processing",
        "execution_id": "4711",
        "message": f"test failure {uuid.uuid4()}",
        **body,
    }
    return http.post("/alerts", json=payload, headers={**headers, "Idempotency-Key": key})


def test_a_failure_alert_reaches_the_alerts_channel_escaped_and_redacted(client, monkeypatch):
    from policy_service.integrations import slack_client
    from policy_service.integrations.slack_client import PostOutcome

    http, headers = client
    slack = ScriptedSlack(PostOutcome(True, message_ts="1.1"))
    monkeypatch.setattr(slack_client, "SlackClient", lambda **_kwargs: slack)

    response = _alert(
        http,
        headers,
        f"alert-test-{uuid.uuid4()}",
        failed_node="Notify Failed",
        message=f"notify failed: 502 <!channel> xoxb-1234567890-abcdefghijkl {uuid.uuid4()}",
    )

    assert response.status_code == 200
    (sent,) = slack.sent
    rendered = str(sent["blocks"])
    assert sent["channel"] == "ap-alerts"
    assert "02-bill-processing" in rendered and "Notify Failed" in rendered
    assert "<!channel>" not in rendered
    assert "xoxb-1234567890-abcdefghijkl" not in rendered


def test_an_alert_that_was_not_posted_is_a_502_and_can_be_retried(client, monkeypatch):
    from policy_service.integrations import slack_client
    from policy_service.integrations.slack_client import PostOutcome

    http, headers = client
    slack = ScriptedSlack(PostOutcome(False, error="not_in_channel"), PostOutcome(True))
    monkeypatch.setattr(slack_client, "SlackClient", lambda **_kwargs: slack)
    key = f"alert-test-{uuid.uuid4()}"
    message = f"test failure {uuid.uuid4()}"  # the same request each time, as a retry is

    first = _alert(http, headers, key, message=message)
    recorded = _registry_row(key)
    second = _alert(http, headers, key, message=message)
    replay = _alert(http, headers, key, message=message)

    assert first.status_code == 502
    assert first.json()["error"] == "ALERT_NOT_POSTED"
    assert recorded == ("FAILED", "RETRYABLE", "ALERT_NOT_POSTED")
    assert second.status_code == 200
    assert replay.headers.get("Idempotency-Replayed") == "true"
    assert slack.posts == 2


# --- stale model attempts are recovered by the poll ----------------------------
class EmptyXero:
    def list_invoices(self, **_kwargs):
        return {"Invoices": []}


def test_a_poll_finalises_a_stale_attempt_and_sends_its_run_back(client, monkeypatch):
    from policy_service.api import deps
    from policy_service.domain import semantic
    from tests.integration.test_semantic_lifecycle import _seed_wording_run

    http, headers = client
    with psycopg.connect(OWNER_URL) as conn:
        run_id, _ = _seed_wording_run(conn)
        conn.commit()
        semantic.start_attempt(
            conn, run_id=run_id, correlation_id=uuid.uuid4(), model_id="m", enabled_flag=True
        )
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE semantic_attempts SET started_at = now() - interval '11 minutes' "
                "WHERE run_id = %s",
                (run_id,),
            )
        conn.commit()
    monkeypatch.setattr(deps, "get_xero_client", lambda: EmptyXero())

    response = http.post(
        "/runs/poll", headers={**headers, "Idempotency-Key": f"poll-test-{uuid.uuid4()}"}
    )

    assert response.status_code == 200
    assert response.json()["recovered"] >= 1
    assert str(run_id) in response.json()["run_ids"]


def test_a_long_error_message_is_shortened_not_refused(client, monkeypatch):
    """Over 2,000 characters used to be a 422, so the alert was never posted."""
    from policy_service.integrations import slack_client
    from policy_service.integrations.slack_client import PostOutcome

    http, headers = client
    slack = ScriptedSlack(PostOutcome(True))
    monkeypatch.setattr(slack_client, "SlackClient", lambda **_kwargs: slack)

    response = _alert(
        http, headers, f"alert-test-{uuid.uuid4()}", message=f"{uuid.uuid4()} " + "x" * 5000
    )

    assert response.status_code == 200
    assert slack.posts == 1


def test_a_repeated_alert_is_recorded_but_not_posted_again(client, monkeypatch):
    from policy_service.integrations import slack_client
    from policy_service.integrations.slack_client import PostOutcome

    http, headers = client
    slack = ScriptedSlack(PostOutcome(True), PostOutcome(True))
    monkeypatch.setattr(slack_client, "SlackClient", lambda **_kwargs: slack)
    message = f"poll failed: 503 XERO_UNAVAILABLE {uuid.uuid4()}"

    first = _alert(http, headers, f"alert-test-{uuid.uuid4()}", message=message)
    repeat = _alert(http, headers, f"alert-test-{uuid.uuid4()}", message=message)
    different = _alert(http, headers, f"alert-test-{uuid.uuid4()}", message=message + " other")

    assert first.json()["posted"] is True
    assert repeat.status_code == 200
    assert repeat.json() | {"correlation_id": None} == repeat.json() | {
        "posted": False,
        "suppressed": True,
        "correlation_id": None,
    }
    assert different.json()["posted"] is True
    assert slack.posts == 2


def test_one_stale_attempt_that_cannot_be_finalised_does_not_stop_the_poll(client, monkeypatch):
    """It ran inside the poll transaction, so its error failed the poll, and the
    next one, and every one after."""
    from policy_service.api import deps
    from policy_service.domain import semantic
    from tests.integration.test_semantic_lifecycle import _seed_started_attempt

    http, headers = client
    with psycopg.connect(OWNER_URL) as conn:
        broken_run, broken_attempt = _seed_started_attempt(conn, age_minutes=11)
        good_run, _ = _seed_started_attempt(conn, age_minutes=11)

    real_finalise = semantic.finalise

    def finalise(conn, *, attempt_id, **kwargs):
        if attempt_id == broken_attempt:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 / 0")  # a real database error, which aborts the transaction
        return real_finalise(conn, attempt_id=attempt_id, **kwargs)

    monkeypatch.setattr(semantic, "finalise", finalise)
    monkeypatch.setattr(deps, "get_xero_client", lambda: EmptyXero())

    response = http.post(
        "/runs/poll", headers={**headers, "Idempotency-Key": f"poll-test-{uuid.uuid4()}"}
    )
    with psycopg.connect(OWNER_URL) as conn, conn.cursor() as cur:
        cur.execute("SELECT status FROM semantic_attempts WHERE attempt_id = %s", (broken_attempt,))
        broken_status = cur.fetchone()[0]

    assert response.status_code == 200
    assert str(good_run) in response.json()["run_ids"]
    assert str(broken_run) not in response.json()["run_ids"]
    assert broken_status == "STARTED"  # left for the next poll


def test_the_poll_resends_runs_still_waiting_for_a_card(client, monkeypatch):
    from policy_service.api import deps
    from policy_service.domain import poller

    http, headers = client
    waiting = uuid.uuid4()
    monkeypatch.setattr(poller, "runs_awaiting_a_card", lambda conn: [waiting])
    monkeypatch.setattr(deps, "get_xero_client", lambda: EmptyXero())

    response = http.post(
        "/runs/poll", headers={**headers, "Idempotency-Key": f"poll-test-{uuid.uuid4()}"}
    )

    assert response.status_code == 200
    assert response.json()["resent"] == 1
    assert str(waiting) in response.json()["run_ids"]


# --- hand-off through the real Slack and notify endpoints ---------------------
class HandoffSlack(ScriptedSlack):
    def __init__(self, *outcomes) -> None:
        super().__init__(*outcomes)
        self.updates: list[dict] = []

    def update_card(self, **kwargs):
        from policy_service.integrations.slack_client import PostOutcome

        self.updates.append(kwargs)
        return PostOutcome(True)


def _signed_click(http, run_id, action, message_ts, secret, channel="C-AP"):
    import json
    import time
    from urllib.parse import quote_plus

    from policy_service.security.hmac_verify import compute_signature

    payload = {
        "type": "block_actions",
        "trigger_id": f"t-{uuid.uuid4()}",
        "user": {"id": "U123", "username": "ap.reviewer"},
        "message": {"ts": message_ts},
        "channel": {"id": channel},
        "actions": [{"action_id": action, "value": str(run_id)}],
    }
    body = f"payload={quote_plus(json.dumps(payload))}".encode()
    timestamp = str(int(time.time()))
    return http.post(
        "/slack/interactions",
        content=body,
        headers={
            "content-type": "application/x-www-form-urlencoded",
            "x-slack-request-timestamp": timestamp,
            "x-slack-signature": compute_signature(secret, timestamp, body),
        },
    )


def test_a_send_to_finance_click_posts_the_case_to_the_finance_channel(client, monkeypatch):
    """The reported bug: the click was recorded, the AP card updated, and
    nothing ever reached the finance channel."""
    from policy_service.config import get_settings
    from policy_service.domain import triage
    from policy_service.integrations import slack_client
    from policy_service.integrations.slack_client import PostOutcome
    from tests.integration.test_triage import AP_TS, FINANCE_TS, FakeSlack, _seed_ap_review

    http, _ = client
    secret = "test-signing-value-not-real"
    monkeypatch.setenv("SLACK_SIGNING_SECRET", secret)
    get_settings.cache_clear()
    with psycopg.connect(OWNER_URL) as conn:
        run_id = _seed_ap_review(conn)
        triage.notify(
            conn,
            run_id=run_id,
            correlation_id=uuid.uuid4(),
            client=FakeSlack(post_result=PostOutcome(True, message_ts=AP_TS, channel_id="C-AP")),
        )
        conn.commit()
    fake = HandoffSlack(PostOutcome(True, message_ts=FINANCE_TS, channel_id="C-FIN"))
    monkeypatch.setattr(slack_client, "SlackClient", lambda **_kwargs: fake)

    response = _signed_click(http, run_id, "SEND_TO_FINANCE", AP_TS, secret)

    assert response.status_code == 200
    (update,) = fake.updates
    assert update["message_ts"] == AP_TS and "now with finance" in str(update["blocks"])
    (post,) = fake.sent
    assert post["channel"] == "ap-finance"
    assert "Sent here from ap review" in str(post["blocks"])


def test_a_resend_after_a_failed_hand_off_card_posts_it_instead_of_replaying(client, monkeypatch):
    """The notify key had already succeeded for the AP card, so the re-send
    replayed "posted" and finance's card never went out."""
    from policy_service.domain import triage
    from policy_service.domain.enums import TriageAction
    from policy_service.integrations import slack_client
    from policy_service.integrations.slack_client import PostOutcome
    from tests.integration.test_triage import AP_TS, FINANCE_TS, _decide, _seed_ap_review

    http, headers = client
    with psycopg.connect(OWNER_URL) as conn:
        run_id = _seed_ap_review(conn)
    key = f"{run_id}-notify"
    slack = ScriptedSlack(
        PostOutcome(True, message_ts=AP_TS, channel_id="C-AP"),
        PostOutcome(True, message_ts=FINANCE_TS, channel_id="C-FIN"),
    )
    monkeypatch.setattr(slack_client, "SlackClient", lambda **_kwargs: slack)

    first = http.post(f"/runs/{run_id}/notify", headers={**headers, "Idempotency-Key": key})
    with psycopg.connect(OWNER_URL) as conn:
        _decide(conn, run_id, TriageAction.SEND_TO_FINANCE, AP_TS)  # its card post "failed"
        assert triage.awaits_card(conn, run_id) is True
    resend = http.post(f"/runs/{run_id}/notify", headers={**headers, "Idempotency-Key": key})

    assert first.json()["channel"] == "ap-review"
    assert resend.status_code == 200
    assert "Idempotency-Replayed" not in resend.headers
    assert resend.json()["posted"] is True and resend.json()["channel"] == "ap-finance"
    assert [s["channel"] for s in slack.sent] == ["ap-review", "ap-finance"]


def test_a_click_on_a_card_already_decided_refreshes_it_instead_of_failing(client, monkeypatch):
    """A card whose update was lost kept its buttons, and every click on it
    was a 409 that Slack showed as an error."""
    from policy_service.config import get_settings
    from policy_service.domain import triage
    from policy_service.domain.enums import TriageAction
    from policy_service.integrations import slack_client
    from policy_service.integrations.slack_client import PostOutcome
    from tests.integration.test_triage import AP_TS, FakeSlack, _decide, _seed_ap_review

    http, _ = client
    secret = "test-signing-value-not-real"
    monkeypatch.setenv("SLACK_SIGNING_SECRET", secret)
    get_settings.cache_clear()
    with psycopg.connect(OWNER_URL) as conn:
        run_id = _seed_ap_review(conn)
        triage.notify(
            conn,
            run_id=run_id,
            correlation_id=uuid.uuid4(),
            client=FakeSlack(post_result=PostOutcome(True, message_ts=AP_TS, channel_id="C-AP")),
        )
        conn.commit()
        # Decided, and its card update "lost": the card still has buttons.
        _decide(conn, run_id, TriageAction.REQUEST_MORE_INFORMATION, AP_TS)
    fake = HandoffSlack()
    monkeypatch.setattr(slack_client, "SlackClient", lambda **_kwargs: fake)

    response = _signed_click(http, run_id, "MARK_REVIEWED", AP_TS, secret, channel="C-AP")

    assert response.status_code == 200
    (update,) = fake.updates
    assert (update["channel"], update["message_ts"]) == ("C-AP", AP_TS)
    assert "Request more information" in str(update["blocks"])  # the recorded decision
