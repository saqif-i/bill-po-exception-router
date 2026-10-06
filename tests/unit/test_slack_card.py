"""The triage card, and the wording that is invariant I03.

Invariant I03. Pure: no Slack workspace, no network.

I03 says Slack controls express operational triage decisions, not financial
approval. That invariant lives in the button labels, so the labels are what a
test asserts.
"""

from __future__ import annotations

import json
import pathlib

from policy_service.domain.enums import TriageAction, TriageDestination
from policy_service.domain.triage import allowed_actions
from policy_service.integrations.slack_blocks import (
    ACTION_LABELS,
    ACTIONS_FOR,
    CHANNEL_FOR,
    DONE_LABELS,
    MAX_BLOCKS,
    MODAL_TEXT,
    SECTION_LIMIT,
    build_card,
    build_decided_card,
    build_note_modal,
    case_path,
    escape_mrkdwn,
)

A = TriageAction
D = TriageDestination


def test_no_control_is_named_approve_or_reject() -> None:
    """I03: Slack controls express operational triage, not financial approval.
    If this test ever needs relaxing, the project has changed into something
    else. The modals' titles and buttons are controls too."""
    modal_words = [word for title, submit, _ in MODAL_TEXT.values() for word in (title, submit)]
    for label in [*ACTION_LABELS.values(), *DONE_LABELS.values(), *modal_words]:
        lowered = label.lower()
        assert "approve" not in lowered
        assert "reject" not in lowered
        assert "pay" not in lowered


def test_every_action_has_a_label_and_reads_as_done_afterwards() -> None:
    assert set(ACTION_LABELS) == set(TriageAction)
    assert set(DONE_LABELS) == set(TriageAction)


def test_modal_titles_and_buttons_fit_slack_limits() -> None:
    """Slack refuses a modal whose title or button text passes 24 characters,
    and the person clicking sees nothing open."""
    for title, submit, _question in MODAL_TEXT.values():
        assert len(title) <= 24 and len(submit) <= 24


def test_every_destination_has_controls_and_a_channel() -> None:
    for destination in TriageDestination:
        assert ACTIONS_FOR[destination]
        assert CHANNEL_FOR[destination]


def test_duplicate_review_offers_closing_it_as_a_duplicate() -> None:
    assert TriageAction.CLOSE_AS_DUPLICATE in ACTIONS_FOR[TriageDestination.DUPLICATE_REVIEW]


def test_a_card_without_a_recommendation_explains_the_absence() -> None:
    """I06: nothing is shown rather than a hedge, and the reason is visible."""
    blocks = build_card(
        run_id="r1",
        invoice_number="INV-1002",
        destination=TriageDestination.PROCUREMENT,
        exception_codes=["QUANTITY_VARIANCE"],
        recommendation=None,
        semantic_gate_reason="HEADER_OR_PAIRED_LINE_EXCEPTION",
        human_review_reasons=["DETERMINISTIC_EXCEPTION"],
    )
    rendered = str(blocks)
    assert "HEADER_OR_PAIRED_LINE_EXCEPTION" in rendered
    assert "LIKELY_EQUIVALENT" not in rendered


def test_a_card_with_a_recommendation_labels_it_as_context() -> None:
    blocks = build_card(
        run_id="r1",
        invoice_number="INV-1008",
        destination=TriageDestination.AP_REVIEW,
        exception_codes=["UNPAIRED_LINE"],
        recommendation={
            "recommendation": "LIKELY_EQUIVALENT",
            "confidence": 0.91,
            "explanation": "Both describe bottled water in a case of 24.",
            "evidence": [{"source": "BILL_LINE", "text": "bottled water"}],
        },
        semantic_gate_reason="RESIDUAL_PAIR_TEXT_ONLY",
        human_review_reasons=["SEMANTIC_RECOMMENDATION_AVAILABLE"],
    )
    rendered = str(blocks)
    assert "not a decision" in rendered
    assert "bottled water" in rendered


def test_every_card_carries_the_non_approval_disclaimer() -> None:
    for recommendation in (
        None,
        {
            "recommendation": "LIKELY_DIFFERENT",
            "confidence": 0.8,
            "explanation": "Different items.",
            "evidence": [{"source": "BILL_LINE", "text": "water"}],
        },
    ):
        blocks = build_card(
            run_id="r1",
            invoice_number="INV-1",
            destination=TriageDestination.AP_REVIEW,
            exception_codes=["UNPAIRED_LINE"],
            recommendation=recommendation,
            semantic_gate_reason="RESIDUAL_PAIR_TEXT_ONLY",
            human_review_reasons=[],
        )
        assert "does not approve, reject or pay anything" in str(blocks)


def test_supplier_and_model_text_cannot_ping_or_disguise_links() -> None:
    """Evidence is verbatim supplier text. Unescaped, `<!channel>` would notify
    everyone in the channel."""
    blocks = build_card(
        run_id="r1",
        invoice_number="INV-1008",
        destination=TriageDestination.AP_REVIEW,
        exception_codes=["UNPAIRED_LINE"],
        recommendation={
            "recommendation": "LIKELY_EQUIVALENT",
            "confidence": 0.91,
            "explanation": "See <https://evil.example|the invoice> & <!here>",
            "evidence": [{"source": "BILL_LINE", "text": "water <!channel>"}],
        },
        semantic_gate_reason="RESIDUAL_PAIR_TEXT_ONLY",
        human_review_reasons=["SEMANTIC_RECOMMENDATION_AVAILABLE"],
    )
    rendered = str(blocks)
    assert "<!channel>" not in rendered
    assert "<!here>" not in rendered
    assert "<https://evil.example" not in rendered
    assert "water &lt;!channel&gt;" in rendered
    assert "&amp; &lt;!here&gt;" in rendered


def test_escaping_does_not_double_escape() -> None:
    assert escape_mrkdwn("a & b < c > d") == "a &amp; b &lt; c &gt; d"


def test_a_handed_off_card_says_where_the_case_went() -> None:
    from policy_service.integrations.slack_blocks import build_decided_card

    handed = str(
        build_decided_card(
            invoice_number="INV-1",
            action=TriageAction.SEND_TO_FINANCE,
            decided_by="U1",
            decided_at="now",
            handed_to=TriageDestination.FINANCE,
        )
    )
    final = str(
        build_decided_card(
            invoice_number="INV-1",
            action=TriageAction.MARK_REVIEWED,
            decided_by="U1",
            decided_at="now",
        )
    )
    assert "It is now with finance." in handed
    assert "routed" not in final and "now with" not in final


def test_a_card_handed_to_a_team_says_who_sent_it() -> None:
    blocks = build_card(
        run_id="r1",
        invoice_number="INV-1",
        destination=TriageDestination.FINANCE,
        exception_codes=["UNPAIRED_LINE"],
        recommendation=None,
        semantic_gate_reason="RESIDUAL_PAIR_TEXT_ONLY",
        human_review_reasons=[],
        handed_over={"by": "U123", "from": TriageDestination.AP_REVIEW},
    )
    assert "Sent here from AP review by <@U123>." in str(blocks)


# --- escalation and information requests (ADR-010) ---------------------------
def test_escalate_is_offered_where_it_was_and_never_twice() -> None:
    """I42. Not on AP review, not on the escalations card, and not on the card
    a send-back creates."""
    fresh = {"information_open": False, "escalated": False}
    assert A.ESCALATE not in allowed_actions(D.AP_REVIEW, **fresh)
    for destination in (D.FINANCE, D.PROCUREMENT, D.DUPLICATE_REVIEW):
        assert A.ESCALATE in allowed_actions(destination, **fresh)
        assert A.ESCALATE not in allowed_actions(
            destination, information_open=False, escalated=True
        )
    assert allowed_actions(D.ESCALATED, information_open=False, escalated=True) == (
        A.MARK_REVIEWED,
        A.SEND_BACK,
        A.REQUEST_MORE_INFORMATION,
    )


def test_an_open_request_leaves_only_the_answer_and_escalate() -> None:
    assert allowed_actions(D.FINANCE, information_open=True, escalated=False) == (
        A.INFORMATION_RECEIVED,
        A.ESCALATE,
    )
    assert allowed_actions(D.AP_REVIEW, information_open=True, escalated=False) == (
        A.INFORMATION_RECEIVED,
    )
    assert allowed_actions(D.FINANCE, information_open=True, escalated=True) == (
        A.INFORMATION_RECEIVED,
    )
    assert allowed_actions(D.ESCALATED, information_open=True, escalated=True) == (
        A.INFORMATION_RECEIVED,
    )


def _entry(action, note=None, *, by="U1", source=D.FINANCE, to=None):
    return {
        "action": action,
        "by": by,
        "at": "07 Oct 2026, 10:00 UTC",
        "from": source,
        "to": to,
        "note": note,
    }


def test_the_case_path_names_every_move() -> None:
    history = [
        _entry(A.SEND_TO_FINANCE, source=D.AP_REVIEW, to=D.FINANCE),
        _entry(A.REQUEST_MORE_INFORMATION, "Which cost centre?"),
        _entry(A.ESCALATE, "Disputed", to=D.ESCALATED),
        _entry(A.SEND_BACK, "Use the PO price", source=D.ESCALATED, to=D.FINANCE),
    ]
    assert case_path(history) == (
        "AP review → sent to finance → escalated by finance → sent back to finance"
    )
    assert case_path([_entry(A.REQUEST_MORE_INFORMATION, "Which?")]) is None
    assert case_path([]) is None
    # A row from before hand-offs existed, when "send to finance" was final.
    assert case_path([_entry(A.SEND_TO_FINANCE, source=D.AP_REVIEW)]) is None


HOSTILE = "<!channel> see <https://x|y> and <@U999>"


def _inert(blocks) -> None:
    rendered = str(blocks)
    assert "<!channel>" not in rendered
    assert "<https://x|y>" not in rendered
    assert "<@U999>" not in rendered
    assert "&lt;!channel&gt;" in rendered
    for block in blocks:
        text = block.get("text") or {}
        if "&lt;!channel&gt;" in text.get("text", ""):
            assert text.get("verbatim") is True, "a note's section must not be auto-parsed"


def test_notes_cannot_ping_or_disguise_links_on_any_card() -> None:
    """I43. A note is typed by a person, and every card that shows one escapes
    it: the escalation card, the send-back card, the waiting card, the card
    a decision replaces and the final card."""
    history = [
        _entry(A.REQUEST_MORE_INFORMATION, HOSTILE),
        _entry(A.INFORMATION_RECEIVED, HOSTILE),
        _entry(A.ESCALATE, HOSTILE, to=D.ESCALATED),
    ]
    card = {
        "run_id": "r1",
        "invoice_number": "INV-1",
        "exception_codes": ["QUANTITY_VARIANCE"],
        "recommendation": None,
        "semantic_gate_reason": "HEADER_OR_PAIRED_LINE_EXCEPTION",
        "human_review_reasons": [],
    }
    escalated = build_card(
        **card, destination=D.ESCALATED, history=history, handed_over=history[-1]
    )
    sent_back = _entry(A.SEND_BACK, HOSTILE, source=D.ESCALATED, to=D.FINANCE)
    returned = build_card(
        **card, destination=D.FINANCE, history=[*history, sent_back], handed_over=sent_back
    )
    waiting = build_card(
        **card,
        destination=D.FINANCE,
        history=history[:1],
        waiting={"question": HOSTILE, "by": "U1", "at": "now"},
    )
    replaced = build_decided_card(
        invoice_number="INV-1",
        action=A.ESCALATE,
        decided_by="U1",
        decided_at="now",
        handed_to=D.ESCALATED,
        note=HOSTILE,
    )
    final = build_decided_card(
        invoice_number="INV-1",
        action=A.MARK_REVIEWED,
        decided_by="U1",
        decided_at="now",
        history=[*history, sent_back],
    )
    for blocks in (escalated, returned, waiting, replaced, final):
        _inert(blocks)


def test_the_escalation_card_shows_who_when_why_and_the_path() -> None:
    escalation = _entry(A.ESCALATE, "Supplier disputes the price", by="U200", to=D.ESCALATED)
    blocks = build_card(
        run_id="r1",
        invoice_number="INV-1",
        destination=D.ESCALATED,
        exception_codes=["QUANTITY_VARIANCE"],
        recommendation=None,
        semantic_gate_reason="HEADER_OR_PAIRED_LINE_EXCEPTION",
        human_review_reasons=[],
        handed_over=escalation,
        history=[escalation],
        po_number="PO-0042",
    )
    rendered = str(blocks)
    assert "*Escalated from finance* by <@U200> at 07 Oct 2026, 10:00 UTC" in rendered
    assert "> Supplier disputes the price" in rendered
    assert "finance → escalated by finance" in rendered
    assert "PO-0042" in rendered
    assert CHANNEL_FOR[D.ESCALATED] == "ap-escalations"


def test_the_card_a_decision_replaces_says_who_escalated_and_why() -> None:
    rendered = str(
        build_decided_card(
            invoice_number="INV-1",
            action=A.ESCALATE,
            decided_by="U200",
            decided_at="now",
            handed_to=D.ESCALATED,
            note="Supplier disputes the price",
        )
    )
    assert "*Escalated* by <@U200>: Supplier disputes the price" in rendered
    assert "It is now with escalations." in rendered
    assert "'actions'" not in rendered  # its buttons are gone


def test_the_modal_asks_for_one_bounded_note_and_carries_the_card() -> None:
    modal = build_note_modal(
        A.SEND_BACK,
        run_id="r1",
        invoice_number="INV-1",
        message_ts="1.2",
        channel="C1",
        team="procurement",
    )
    (note,) = [b for b in modal["blocks"] if b["type"] == "input"]
    assert note["element"]["min_length"] == 1
    assert note["element"]["max_length"] == 500
    assert note["label"]["text"] == "What should procurement know?"
    assert json.loads(modal["private_metadata"]) == {
        "run_id": "r1",
        "action": "SEND_BACK",
        "message_ts": "1.2",
        "channel": "C1",
    }
    question = build_note_modal(
        A.REQUEST_MORE_INFORMATION, run_id="r1", invoice_number="I", message_ts="1", channel="C"
    )
    assert "What information is needed, and from whom?" in str(question)


def test_a_long_history_stays_inside_slack_limits_and_says_what_it_left_out() -> None:
    """Every note can escape to four times its length. Slack refuses a section
    over 3000 characters or a message over 50 blocks, and a refused update
    leaves the card stale."""
    rounds = [
        _entry(action, "<" * 500)
        for _ in range(40)
        for action in (A.REQUEST_MORE_INFORMATION, A.INFORMATION_RECEIVED)
    ]
    blocks = build_card(
        run_id="r1",
        invoice_number="INV-1",
        destination=D.FINANCE,
        exception_codes=["QUANTITY_VARIANCE"],
        recommendation=None,
        semantic_gate_reason="HEADER_OR_PAIRED_LINE_EXCEPTION",
        human_review_reasons=[],
        history=rounds,
    )
    assert len(blocks) <= MAX_BLOCKS
    assert all(len((b.get("text") or {}).get("text", "")) <= SECTION_LIMIT for b in blocks)
    assert "earlier entries are in the decision record" in str(blocks)
    short = build_card(
        run_id="r1",
        invoice_number="INV-1",
        destination=D.FINANCE,
        exception_codes=["QUANTITY_VARIANCE"],
        recommendation=None,
        semantic_gate_reason="HEADER_OR_PAIRED_LINE_EXCEPTION",
        human_review_reasons=[],
        history=rounds[:4],
    )
    assert "earlier entries" not in str(short)


def test_notes_never_reach_a_model() -> None:
    """I43, and docs/security.md. The code that receives, stores and shows a
    note never imports the model client."""
    repo = pathlib.Path(__file__).resolve().parents[2] / "policy_service"
    for path in ("domain/triage.py", "api/slack.py", "integrations/slack_blocks.py"):
        source = (repo / path).read_text()
        assert "claude_client" not in source and "anthropic" not in source.lower(), path
