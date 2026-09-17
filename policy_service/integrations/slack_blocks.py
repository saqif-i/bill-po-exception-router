"""The triage card (Volume 09 sections 9.4 and 9.6).

Invariant I03: Slack controls express operational triage decisions, not
financial approval. **No control is named Approve or Reject**, because none of
them is one. The wording here is the invariant's enforcement point.
"""

from __future__ import annotations

from policy_service.domain.enums import TriageAction, TriageDestination

# What each control means to the person clicking it. These strings appear on
# the card and are the reason a reviewer cannot mistake this for an approval
# queue.
ACTION_LABELS: dict[TriageAction, str] = {
    TriageAction.MARK_REVIEWED: "Mark reviewed",
    TriageAction.SEND_TO_FINANCE: "Send to finance",
    TriageAction.SEND_TO_PROCUREMENT: "Send to procurement",
    TriageAction.REQUEST_MORE_INFORMATION: "Request more information",
    TriageAction.ESCALATE: "Escalate",
    TriageAction.CLOSE_AS_DUPLICATE: "Close as duplicate",
}

CHANNEL_FOR: dict[TriageDestination, str] = {
    TriageDestination.FINANCE: "ap-finance",
    TriageDestination.PROCUREMENT: "ap-procurement",
    TriageDestination.AP_REVIEW: "ap-review",
    TriageDestination.DUPLICATE_REVIEW: "ap-duplicates",
}

# Which controls make sense for which destination. A duplicate-review card
# showing "Send to procurement" invites a decision that does not fit the case.
ACTIONS_FOR: dict[TriageDestination, tuple[TriageAction, ...]] = {
    TriageDestination.FINANCE: (
        TriageAction.MARK_REVIEWED,
        TriageAction.REQUEST_MORE_INFORMATION,
        TriageAction.ESCALATE,
    ),
    TriageDestination.PROCUREMENT: (
        TriageAction.MARK_REVIEWED,
        TriageAction.REQUEST_MORE_INFORMATION,
        TriageAction.ESCALATE,
    ),
    TriageDestination.AP_REVIEW: (
        TriageAction.MARK_REVIEWED,
        TriageAction.SEND_TO_FINANCE,
        TriageAction.SEND_TO_PROCUREMENT,
        TriageAction.REQUEST_MORE_INFORMATION,
    ),
    TriageDestination.DUPLICATE_REVIEW: (
        TriageAction.CLOSE_AS_DUPLICATE,
        TriageAction.MARK_REVIEWED,
        TriageAction.ESCALATE,
    ),
}

DISCLAIMER = (
    "This is an operational triage record. It does not approve, reject or pay "
    "anything, and it changes nothing in Xero."
)


def _section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def build_card(
    *,
    run_id: str,
    invoice_number: str,
    destination: TriageDestination,
    exception_codes: list[str],
    recommendation: dict | None,
    semantic_gate_reason: str,
    human_review_reasons: list[str],
) -> list[dict]:
    """The card a reviewer sees.

    `recommendation` is None whenever the model was not consulted OR its output
    was rejected. In both cases the card shows nothing rather than a hedge
    (invariant I06), and the gate reason explains the absence.
    """
    codes = ", ".join(f"`{code}`" for code in exception_codes) or "none"
    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"Bill {invoice_number} needs review"},
        },
        _section(f"*Exceptions*\n{codes}"),
        _section(f"*Routed to*  {destination.value.replace('_', ' ').lower()}"),
    ]

    if recommendation is not None:
        spans = "\n".join(
            f"> {span['text']}  _({span['source'].replace('_', ' ').lower()})_"
            for span in recommendation["evidence"]
        )
        blocks.append(
            _section(
                f"*Wording check*  `{recommendation['recommendation']}`  "
                f"(confidence {recommendation['confidence']:.2f})\n"
                f"{recommendation['explanation']}\n{spans}"
            )
        )
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": (
                            "This is context for your decision, not a decision. "
                            "It was produced only because every number already "
                            "agreed."
                        ),
                    }
                ],
            }
        )
    else:
        # Why there is no recommendation is not the same question as why the
        # gate opened or closed, and the reviewer needs the first.
        #
        # When the gate CLOSED, the gate reason is the answer. When it opened
        # and the attempt was then rejected, the gate reason says
        # RESIDUAL_PAIR_TEXT_ONLY, which reads as "a recommendation was
        # possible" beside a card showing none. The rejection reason on the run
        # is the real answer, so it wins.
        rejection = next(
            (
                reason
                for reason in human_review_reasons
                if reason.startswith("SEMANTIC_") and reason != "SEMANTIC_RECOMMENDATION_AVAILABLE"
            ),
            None,
        )
        why = rejection or semantic_gate_reason
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": f"No wording recommendation. Reason: `{why}`"}
                ],
            }
        )

    blocks.append(
        {
            "type": "actions",
            "block_id": f"triage:{run_id}",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": ACTION_LABELS[action]},
                    "action_id": action.value,
                    "value": run_id,
                }
                for action in ACTIONS_FOR[destination]
            ],
        }
    )
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": DISCLAIMER}]})
    return blocks


def build_decided_card(
    *,
    invoice_number: str,
    action: TriageAction,
    decided_by: str,
    decided_at: str,
    destination: TriageDestination,
) -> list[dict]:
    """The card after a decision, replacing the buttons.

    Best effort (ADR-007). The database is authoritative and this is a view; a
    lost response can leave it stale while the decision is correctly recorded.
    """
    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"Bill {invoice_number} triaged"},
        },
        _section(
            f"*{ACTION_LABELS[action]}*\n"
            f"by <@{decided_by}> at {decided_at}\n"
            f"was routed to {destination.value.replace('_', ' ').lower()}"
        ),
        {"type": "context", "elements": [{"type": "mrkdwn", "text": DISCLAIMER}]},
    ]
