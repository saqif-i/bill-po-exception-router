"""Escalation and information requests through the real Slack endpoint (ADR-010).

Signed requests go to /slack/interactions, with the pool pointed at the test
database and the Slack client faked, so the modals, their submissions and every
card update run without a workspace.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from urllib.parse import quote_plus

import pytest

psycopg = pytest.importorskip("psycopg")

from fastapi.testclient import TestClient  # noqa: E402
from psycopg_pool import ConnectionPool  # noqa: E402

from policy_service.domain.enums import TriageDestination  # noqa: E402
from policy_service.integrations.slack_blocks import TEAM  # noqa: E402
from policy_service.integrations.slack_client import PostOutcome  # noqa: E402
from policy_service.security.hmac_verify import compute_signature  # noqa: E402
from tests.integration.test_triage import (  # noqa: E402
    ESCALATION_TS,
    TEAM_TS,
    _backdate,
    _buttons,
    _post,
    _record,
    _run,
    _seed,
    _seed_ap_review,
    _seed_at,
)

OWNER_URL = os.environ.get("BPR_OWNER_DATABASE_URL")
pytestmark = pytest.mark.skipif(not OWNER_URL, reason="no database configured")

SECRET = "test-signing-value-not-real"
REASON = "Supplier disputes the agreed price"


class ModalSlack:
    """Opens modals, posts cards and updates them. The same instance serves the
    request and the card work that runs after a submission is acknowledged."""

    def __init__(self) -> None:
        self.post_outcomes: list[PostOutcome] = []
        self.view_outcome = PostOutcome(True)
        self.sent: list[dict] = []
        self.updates: list[dict] = []
        self.views: list[dict] = []

    def post_card(self, **kwargs) -> PostOutcome:
        self.sent.append(kwargs)
        if self.post_outcomes:
            return self.post_outcomes.pop(0)
        return PostOutcome(True, message_ts=f"1700000100.{len(self.sent):06d}", channel_id="C-X")

    def update_card(self, **kwargs) -> PostOutcome:
        self.updates.append(kwargs)
        return PostOutcome(True)

    def open_view(self, **kwargs) -> PostOutcome:
        self.views.append(kwargs)
        return self.view_outcome

    def close(self) -> None:
        pass

    def __enter__(self) -> ModalSlack:
        return self

    def __exit__(self, *_exc: object) -> None:
        pass


@pytest.fixture
def http(monkeypatch: pytest.MonkeyPatch):
    from policy_service.api import runs, slack
    from policy_service.config import get_settings
    from policy_service.main import app

    monkeypatch.setenv("SLACK_BOT_TOKEN", "test-slack-token-not-real")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", SECRET)
    get_settings.cache_clear()
    pool = ConnectionPool(OWNER_URL, min_size=1, max_size=2, open=True)
    monkeypatch.setattr(runs, "get_pool", lambda: pool)
    monkeypatch.setattr(slack, "get_pool", lambda: pool)
    yield TestClient(app)
    pool.close()


@pytest.fixture
def slack(monkeypatch: pytest.MonkeyPatch) -> ModalSlack:
    from policy_service.integrations import slack_client

    fake = ModalSlack()
    monkeypatch.setattr(slack_client, "SlackClient", lambda **_kwargs: fake)
    return fake


def _signed(http, payload: dict):
    body = f"payload={quote_plus(json.dumps(payload))}".encode()
    timestamp = str(int(time.time()))
    return http.post(
        "/slack/interactions",
        content=body,
        headers={
            "content-type": "application/x-www-form-urlencoded",
            "x-slack-request-timestamp": timestamp,
            "x-slack-signature": compute_signature(SECRET, timestamp, body),
        },
    )


def _click(http, run_id, action, message_ts, channel="C-TEAM", user="U123"):
    return _signed(
        http,
        {
            "type": "block_actions",
            "trigger_id": f"t-{uuid.uuid4()}",
            "user": {"id": user, "username": "reviewer"},
            "message": {"ts": message_ts},
            "channel": {"id": channel},
            "actions": [{"action_id": action, "value": str(run_id)}],
        },
    )


def _submit(http, modal, note, user="U123", view_id=None):
    return _signed(
        http,
        {
            "type": "view_submission",
            "user": {"id": user, "username": "reviewer"},
            "view": {
                "id": view_id or f"V{uuid.uuid4().hex[:10]}",
                "callback_id": modal["callback_id"],
                "private_metadata": modal["private_metadata"],
                "state": {
                    "values": {"note": {"note": {"type": "plain_text_input", "value": note}}}
                },
            },
        },
    )


def _open(http, slack, run_id, action, message_ts, channel="C-TEAM"):
    """Click a control that asks for a note, and return the modal it opened."""
    response = _click(http, run_id, action, message_ts, channel=channel)
    assert response.status_code == 200, response.text
    return slack.views[-1]["view"]


def _decisions(run_id):
    with psycopg.connect(OWNER_URL) as conn:
        return _record(conn, run_id)


def _state(run_id):
    with psycopg.connect(OWNER_URL) as conn:
        return _run(conn, run_id)


def _seeded_at(destination):
    with psycopg.connect(OWNER_URL) as conn:
        return _seed_at(conn, destination)


# --- Part 1: escalate ---------------------------------------------------------
@pytest.mark.parametrize("destination", ["FINANCE", "PROCUREMENT", "DUPLICATE_REVIEW"])
def test_escalate_asks_for_a_reason_and_moves_the_case_to_escalations(http, slack, destination):
    run_id = _seeded_at(destination)
    slack.post_outcomes = [PostOutcome(True, message_ts=ESCALATION_TS, channel_id="C-ESC")]

    opened = _click(http, run_id, "ESCALATE", TEAM_TS)
    (view,) = slack.views
    modal = view["view"]
    recorded_by_opening = _decisions(run_id)
    submitted = _submit(http, modal, f"  {REASON}  ", user="U200")
    old_card = [_click(http, run_id, action, TEAM_TS) for action in ("MARK_REVIEWED", "ESCALATE")]

    assert opened.status_code == 200
    assert view["trigger_id"].startswith("t-")
    assert json.loads(modal["private_metadata"]) == {
        "run_id": str(run_id),
        "action": "ESCALATE",
        "message_ts": TEAM_TS,
        "channel": "C-TEAM",
    }
    assert recorded_by_opening == []  # opening the modal records nothing
    assert submitted.status_code == 200 and submitted.content == b""  # closes the modal
    assert _decisions(run_id) == [("ESCALATE", REASON, False)]
    assert _state(run_id) == ("AWAITING_TRIAGE", "ESCALATED")

    (update,) = slack.updates  # the card the escalation replaced
    assert update["message_ts"] == TEAM_TS
    assert f"*Escalated* by <@U200>: {REASON}" in str(update["blocks"])
    assert "It is now with escalations." in str(update["blocks"])
    assert _buttons(update["blocks"]) == []

    (card,) = slack.sent
    team = TEAM[TriageDestination(destination)]
    assert card["channel"] == "ap-escalations"
    assert REASON in str(card["blocks"])
    assert f"{team} → escalated by {team}" in str(card["blocks"])
    assert _buttons(card["blocks"]) == ["MARK_REVIEWED", "SEND_BACK", "REQUEST_MORE_INFORMATION"]

    assert [r.status_code for r in old_card] == [409, 409]
    assert [r.json()["error"] for r in old_card] == ["CARD_SUPERSEDED", "CARD_SUPERSEDED"]
    assert len(slack.views) == 1  # no modal for a card that is not current


def test_escalate_is_not_offered_on_ap_review(http, slack):
    with psycopg.connect(OWNER_URL) as conn:
        run_id = _seed_ap_review(conn)
        _post(conn, run_id, TEAM_TS, "C-TEAM")

    response = _click(http, run_id, "ESCALATE", TEAM_TS)

    assert response.status_code == 409
    assert response.json()["error"] == "ACTION_NOT_OFFERED_FOR_AP_REVIEW"
    assert slack.views == []


@pytest.mark.parametrize("note", ["", "   \n  ", "x" * 501])
def test_a_missing_or_over_long_reason_is_sent_back_to_the_modal(http, slack, note):
    run_id = _seeded_at("FINANCE")
    modal = _open(http, slack, run_id, "ESCALATE", TEAM_TS)

    response = _submit(http, modal, note)

    assert response.status_code == 200
    body = response.json()
    assert body["response_action"] == "errors" and body["errors"]["note"]
    assert _decisions(run_id) == []
    assert _state(run_id) == ("AWAITING_TRIAGE", "FINANCE")
    assert slack.sent == [] and slack.updates == []


def test_a_reason_of_exactly_five_hundred_characters_is_kept_trimmed(http, slack):
    run_id = _seeded_at("FINANCE")
    modal = _open(http, slack, run_id, "ESCALATE", TEAM_TS)

    response = _submit(http, modal, "  " + "y" * 500 + "\n")

    assert response.status_code == 200
    ((action, stored, final),) = _decisions(run_id)
    assert (action, stored, final) == ("ESCALATE", "y" * 500, False)


def test_a_case_is_escalated_once(http, slack):
    """I42. Two people open the modal from the same card; the second reason is
    refused. On the escalations card Escalate is refused outright."""
    run_id = _seeded_at("FINANCE")
    slack.post_outcomes = [PostOutcome(True, message_ts=ESCALATION_TS, channel_id="C-ESC")]
    first = _open(http, slack, run_id, "ESCALATE", TEAM_TS)
    second = _open(http, slack, run_id, "ESCALATE", TEAM_TS)

    accepted = _submit(http, first, REASON)
    refused = _submit(http, second, "A second reason")
    on_escalations = _click(http, run_id, "ESCALATE", ESCALATION_TS, channel="C-ESC")

    assert accepted.status_code == 200
    assert refused.status_code == 409 and refused.json()["error"] == "CARD_SUPERSEDED"
    assert on_escalations.status_code == 409
    assert on_escalations.json()["error"] == "ALREADY_ESCALATED"
    assert [row[0] for row in _decisions(run_id)] == ["ESCALATE"]


def test_a_submission_that_arrives_twice_is_recorded_once(http, slack):
    run_id = _seeded_at("FINANCE")
    modal = _open(http, slack, run_id, "ESCALATE", TEAM_TS)

    view_id = f"V-{uuid.uuid4()}"  # one modal; the test database keeps every run's rows
    first = _submit(http, modal, REASON, view_id=view_id)
    again = _submit(http, modal, REASON, view_id=view_id)

    assert first.status_code == 200 and again.status_code == 200
    assert _decisions(run_id) == [("ESCALATE", REASON, False)]
    assert len(slack.sent) == 1  # one escalation card


def test_send_back_and_a_final_decision_from_the_returned_card(http, slack):
    run_id = _seeded_at("PROCUREMENT")
    slack.post_outcomes = [
        PostOutcome(True, message_ts=ESCALATION_TS, channel_id="C-ESC"),
        PostOutcome(True, message_ts="1700000005.000600", channel_id="C-PROC"),
    ]
    _submit(http, _open(http, slack, run_id, "ESCALATE", TEAM_TS), REASON)
    back = _open(http, slack, run_id, "SEND_BACK", ESCALATION_TS, channel="C-ESC")
    _submit(http, back, "Price agreed with the supplier", user="U300")
    escalate_again = _click(http, run_id, "ESCALATE", "1700000005.000600", channel="C-PROC")
    closed = _click(http, run_id, "MARK_REVIEWED", "1700000005.000600", channel="C-PROC")

    assert "What should procurement know?" in str(back)
    returned = slack.sent[1]
    assert returned["channel"] == "ap-procurement"
    assert "Price agreed with the supplier" in str(returned["blocks"])
    assert _buttons(returned["blocks"]) == ["MARK_REVIEWED", "REQUEST_MORE_INFORMATION"]
    assert escalate_again.status_code == 409
    assert closed.status_code == 200
    assert _state(run_id) == ("COMPLETED", "PROCUREMENT")
    assert [(a, f) for a, _n, f in _decisions(run_id)] == [
        ("ESCALATE", False),
        ("SEND_BACK", False),
        ("MARK_REVIEWED", True),
    ]


# --- Part 2: request more information ----------------------------------------
def test_a_request_waits_on_the_card_and_a_lost_update_is_repaired(http, slack):
    run_id = _seeded_at("FINANCE")
    question = _open(http, slack, run_id, "REQUEST_MORE_INFORMATION", TEAM_TS)
    _submit(http, question, "Which cost centre?", user="U400")
    waiting = slack.updates[-1]

    # The same card is clicked as if that update had been lost.
    refused = _click(http, run_id, "MARK_REVIEWED", TEAM_TS)
    repaired = slack.updates[-1]

    answer = _open(http, slack, run_id, "INFORMATION_RECEIVED", TEAM_TS)
    _submit(http, answer, "Cost centre 4410", user="U401")
    restored = slack.updates[-1]

    assert "What information is needed, and from whom?" in str(question)
    assert "What did you find out?" in str(answer)
    assert waiting["message_ts"] == TEAM_TS and slack.sent == []  # no new card
    assert "Waiting for information:* Which cost centre? (asked by <@U400>" in str(
        waiting["blocks"]
    )
    assert _buttons(waiting["blocks"]) == ["INFORMATION_RECEIVED", "ESCALATE"]
    assert refused.status_code == 409 and refused.json()["error"] == "INFORMATION_REQUEST_OPEN"
    assert _buttons(repaired["blocks"]) == ["INFORMATION_RECEIVED", "ESCALATE"]
    assert _buttons(restored["blocks"]) == ["MARK_REVIEWED", "REQUEST_MORE_INFORMATION", "ESCALATE"]
    assert "Which cost centre?" in str(restored["blocks"])
    assert "Cost centre 4410" in str(restored["blocks"])
    assert _state(run_id) == ("AWAITING_TRIAGE", "FINANCE")


def test_a_cancelled_modal_records_nothing(http, slack):
    run_id = _seeded_at("FINANCE")
    modal = _open(http, slack, run_id, "ESCALATE", TEAM_TS)

    closed = _signed(
        http,
        {"type": "view_closed", "user": {"id": "U1"}, "view": {"id": "V1", **modal}},
    )

    assert closed.status_code == 200
    assert _decisions(run_id) == []
    assert _state(run_id) == ("AWAITING_TRIAGE", "FINANCE")


def test_a_submission_with_unreadable_metadata_is_refused(http, slack):
    response = _submit(http, {"callback_id": "triage_note", "private_metadata": "not json"}, "x")

    assert response.status_code == 400


def test_a_modal_slack_will_not_open_is_reported(http, slack):
    run_id = _seeded_at("FINANCE")
    slack.view_outcome = PostOutcome(False, error="expired_trigger_id")

    response = _click(http, run_id, "ESCALATE", TEAM_TS)

    assert response.status_code == 502
    assert response.json()["error"] == "MODAL_NOT_OPENED"
    assert _decisions(run_id) == []


# --- re-send, and escaping end to end -----------------------------------------
def test_the_notify_key_posts_an_escalation_card_that_failed_instead_of_replaying(http, slack):
    """Workflow 02 re-sent by the poll calls notify with the key it used for
    the team's card. That card belongs to a destination the case has left."""
    from policy_service.config import get_settings
    from policy_service.domain.poller import runs_awaiting_a_card

    headers = {"Authorization": f"Bearer {get_settings().internal_bearer_token}"}
    with psycopg.connect(OWNER_URL) as conn:
        run_id = _seed(conn)
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE runs SET triage_destination = 'FINANCE' WHERE run_id = %s", (run_id,)
            )
        conn.commit()
    key = f"{run_id}-notify"
    slack.post_outcomes = [
        PostOutcome(True, message_ts=TEAM_TS, channel_id="C-TEAM"),
        PostOutcome(False, error="not_in_channel"),  # the escalation card
        PostOutcome(True, message_ts=ESCALATION_TS, channel_id="C-ESC"),
    ]
    first = http.post(f"/runs/{run_id}/notify", headers={**headers, "Idempotency-Key": key})
    _submit(http, _open(http, slack, run_id, "ESCALATE", TEAM_TS), REASON)
    waiting = _state(run_id)
    with psycopg.connect(OWNER_URL) as conn:
        _backdate(conn, run_id, 16)
        found = runs_awaiting_a_card(conn, limit=100_000)
    resend = http.post(f"/runs/{run_id}/notify", headers={**headers, "Idempotency-Key": key})

    assert first.json()["channel"] == "ap-finance"
    assert waiting == ("REVIEW_READY", "ESCALATED")
    assert run_id in found
    assert resend.status_code == 200 and "Idempotency-Replayed" not in resend.headers
    assert resend.json()["posted"] is True and resend.json()["channel"] == "ap-escalations"
    assert _state(run_id) == ("AWAITING_TRIAGE", "ESCALATED")


def test_no_note_can_ping_or_disguise_a_link_on_any_card_slack_receives(http, slack):
    """I43, end to end: every card posted or updated through a whole case."""
    hostile = "<!channel> look <https://x|y> <!here>"
    run_id = _seeded_at("FINANCE")
    slack.post_outcomes = [
        PostOutcome(True, message_ts=ESCALATION_TS, channel_id="C-ESC"),
        PostOutcome(True, message_ts="1700000006.000700", channel_id="C-FIN"),
    ]
    _submit(http, _open(http, slack, run_id, "REQUEST_MORE_INFORMATION", TEAM_TS), hostile)
    _submit(http, _open(http, slack, run_id, "INFORMATION_RECEIVED", TEAM_TS), hostile)
    _submit(http, _open(http, slack, run_id, "ESCALATE", TEAM_TS), hostile)
    back = _open(http, slack, run_id, "SEND_BACK", ESCALATION_TS, channel="C-ESC")
    _submit(http, back, hostile)
    closed = _click(http, run_id, "MARK_REVIEWED", "1700000006.000700", channel="C-FIN")

    assert closed.status_code == 200
    cards = [str(m["blocks"]) for m in slack.sent + slack.updates]
    assert len(slack.sent) == 2 and len(slack.updates) == 5
    for rendered in cards:
        assert "<!channel>" not in rendered
        assert "<!here>" not in rendered
        assert "<https://x|y>" not in rendered
    assert "&lt;!channel&gt;" in cards[-1]  # the final card shows the notes, escaped
