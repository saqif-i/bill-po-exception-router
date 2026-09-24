"""The triage card, and the wording that is invariant I03.

Invariant I03. Pure: no Slack workspace, no network.

I03 says Slack controls express operational triage decisions, not financial
approval. That invariant lives in the button labels, so the labels are what a
test asserts.
"""

from __future__ import annotations

from policy_service.domain.enums import TriageAction, TriageDestination
from policy_service.integrations.slack_blocks import (
    ACTION_LABELS,
    ACTIONS_FOR,
    CHANNEL_FOR,
    build_card,
)


def test_no_control_is_named_approve_or_reject() -> None:
    """I03: Slack controls express operational triage, not financial approval.
    If this test ever needs relaxing, the project has changed into something
    else."""
    for label in ACTION_LABELS.values():
        lowered = label.lower()
        assert "approve" not in lowered
        assert "reject" not in lowered
        assert "pay" not in lowered


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
