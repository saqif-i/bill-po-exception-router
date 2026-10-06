"""The triage card.

Invariant I03: Slack controls express operational triage decisions, not
financial approval. **No control is named Approve or Reject**, because none of
them is one. The wording here is the invariant's enforcement point.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

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
    TriageAction.SEND_BACK: "Send back",
    TriageAction.INFORMATION_RECEIVED: "Information received",
}

# How a recorded decision reads afterwards, on the card and in its history.
DONE_LABELS: dict[TriageAction, str] = {
    TriageAction.MARK_REVIEWED: "Marked reviewed",
    TriageAction.SEND_TO_FINANCE: "Sent to finance",
    TriageAction.SEND_TO_PROCUREMENT: "Sent to procurement",
    TriageAction.REQUEST_MORE_INFORMATION: "Information requested",
    TriageAction.ESCALATE: "Escalated",
    TriageAction.CLOSE_AS_DUPLICATE: "Closed as duplicate",
    TriageAction.SEND_BACK: "Sent back",
    TriageAction.INFORMATION_RECEIVED: "Information received",
}

CHANNEL_FOR: dict[TriageDestination, str] = {
    TriageDestination.FINANCE: "ap-finance",
    TriageDestination.PROCUREMENT: "ap-procurement",
    TriageDestination.AP_REVIEW: "ap-review",
    TriageDestination.DUPLICATE_REVIEW: "ap-duplicates",
    TriageDestination.ESCALATED: "ap-escalations",
}

# How each destination is named on a card.
TEAM: dict[TriageDestination, str] = {
    TriageDestination.FINANCE: "finance",
    TriageDestination.PROCUREMENT: "procurement",
    TriageDestination.AP_REVIEW: "AP review",
    TriageDestination.DUPLICATE_REVIEW: "duplicate review",
    TriageDestination.ESCALATED: "escalations",
}

# Which controls make sense for which destination, when nothing is pending. A
# duplicate-review card showing "Send to procurement" invites a decision that
# does not fit the case. What a card actually offers also depends on the case:
# an open information request, or an escalation already made, narrows it. That
# is `policy_service.domain.triage.allowed_actions()`, which cards and clicks
# both go through.
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
    # Reached only by a person's Escalate click, never by routing (I41). No
    # Escalate here: a run is escalated at most once (I42).
    TriageDestination.ESCALATED: (
        TriageAction.MARK_REVIEWED,
        TriageAction.SEND_BACK,
        TriageAction.REQUEST_MORE_INFORMATION,
    ),
}

# Where workflow failures are reported. The bot must be a member, as it must be
# of the five triage channels.
ALERTS_CHANNEL = "ap-alerts"

# The note a person types in a modal: an escalation reason, a send-back note, a
# question or an answer. Bounded here, in the handler and by a CHECK (I43).
NOTE_LIMIT = 500
MODAL_CALLBACK = "triage_note"
NOTE_BLOCK = "note"

# Slack's limits: a section's text, and the blocks in one message.
SECTION_LIMIT = 3000
MAX_BLOCKS = 50

# Title, submit label and question for each control that asks for a note.
# Titles and buttons are plain text of at most 24 characters.
MODAL_TEXT: dict[TriageAction, tuple[str, str, str]] = {
    TriageAction.ESCALATE: ("Escalate", "Escalate", "Why does this need escalating?"),
    TriageAction.SEND_BACK: ("Send back", "Send back", "What should {team} know?"),
    TriageAction.REQUEST_MORE_INFORMATION: (
        "Request information",
        "Request",
        "What information is needed, and from whom?",
    ),
    TriageAction.INFORMATION_RECEIVED: (
        "Information received",
        "Record",
        "What did you find out?",
    ),
}

DISCLAIMER = (
    "This is an operational triage record. It does not approve, reject or pay "
    "anything, and it changes nothing in Xero."
)


def escape_mrkdwn(text: str) -> str:
    """Escape the three characters Slack treats as control sequences.

    Supplier and model text is shown verbatim. Unescaped, a bill line reading
    `<!channel>` would notify the whole channel, and `<https://x|y>` would
    render as a disguised link. `&` goes first so the others are not doubled.
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _section(text: str, *, verbatim: bool = False) -> dict:
    """`verbatim` for text carrying a note a person typed: Slack then leaves a
    bare `@channel` or URL in it alone instead of parsing it."""
    obj = {"type": "mrkdwn", "text": text}
    if verbatim:
        obj["verbatim"] = True
    return {"type": "section", "text": obj}


def _quote(note: str) -> str:
    return "\n".join(f"> {line}" for line in escape_mrkdwn(note).splitlines() or [""])


def decision_line(entry: dict) -> str:
    """One recorded decision as a card shows it: what, who, the note, when.

    `entry` is {"action", "by", "at", "note", "to"}. Every note is escaped.
    """
    action = entry["action"]
    label = DONE_LABELS[action]
    if action is TriageAction.SEND_BACK and entry.get("to") is not None:
        label = f"Sent back to {TEAM[entry['to']]}"
    text = f"*{label}* by <@{entry['by']}>"
    if entry.get("note"):
        text += f": {escape_mrkdwn(entry['note'])}"
    return f"{text}\n_{entry['at']}_"


def case_path(history: Sequence[dict]) -> str | None:
    """Where the case has been, e.g. "AP review → sent to finance → escalated
    by finance". None until it has moved at all."""
    if not history:
        return None
    steps = [TEAM[history[0]["from"]]]
    # Only hand-offs move a case, and only they have somewhere it went ("to").
    for entry in (entry for entry in history if entry.get("to") is not None):
        if entry["action"] is TriageAction.ESCALATE:
            steps.append(f"escalated by {TEAM[entry['from']]}")
        elif entry["action"] is TriageAction.SEND_BACK:
            steps.append(f"sent back to {TEAM[entry['to']]}")
        else:
            steps.append(f"sent to {TEAM[entry['to']]}")
    return " → ".join(steps) if len(steps) > 1 else None


def _pack(lines: Sequence[str]) -> list[str]:
    """As few sections as the 3000-character limit allows. An escaped note is
    at most 2000 characters, so one entry always fits a section of its own."""
    sections: list[str] = []
    for line in lines:
        if sections and len(sections[-1]) + 1 + len(line) <= SECTION_LIMIT:
            sections[-1] += "\n" + line
        else:
            sections.append(line)
    return sections


def _history_blocks(history: Sequence[dict], room: int) -> list[dict]:
    """Every decision so far, every note included.

    Only when the card would pass Slack's 50-block limit are the oldest entries
    left out, and the card says how many. The decision record keeps all of them.
    """
    if not history:
        return []
    lines = [decision_line(entry) for entry in history]
    dropped = 0
    while True:
        head = "*Decisions so far*"
        if dropped:
            head += f"\n_{dropped} earlier entries are in the decision record._"
        sections = _pack([head, *lines[dropped:]])
        if len(sections) <= room or dropped >= len(lines) - 1:
            return [_section(text, verbatim=True) for text in sections]
        dropped += 1


def _arrival(handed_over: dict) -> dict:
    """How the case got to this card. An escalation shows its reason and a
    send-back the note, because they are what the receiving team acts on."""
    action = handed_over.get("action")
    source = TEAM[handed_over["from"]]
    if action is TriageAction.ESCALATE:
        return _section(
            f"*Escalated from {source}* by <@{handed_over['by']}> at {handed_over['at']}\n"
            f"{_quote(handed_over['note'] or '')}",
            verbatim=True,
        )
    if action is TriageAction.SEND_BACK:
        return _section(
            f"*Sent back* by <@{handed_over['by']}> at {handed_over['at']}\n"
            f"{_quote(handed_over['note'] or '')}",
            verbatim=True,
        )
    return {
        "type": "context",
        "elements": [
            {"type": "mrkdwn", "text": f"Sent here from {source} by <@{handed_over['by']}>."}
        ],
    }


def build_card(
    *,
    run_id: str,
    invoice_number: str,
    destination: TriageDestination,
    exception_codes: list[str],
    recommendation: dict | None,
    semantic_gate_reason: str,
    human_review_reasons: list[str],
    handed_over: dict | None = None,
    actions: Sequence[TriageAction] | None = None,
    po_number: str | None = None,
    history: Sequence[dict] = (),
    waiting: dict | None = None,
) -> list[dict]:
    """The card a reviewer sees.

    `actions` are the controls the case allows now, from
    `triage.allowed_actions()`; left out, the destination's full set.

    `handed_over` is the latest hand-off ({"action", "by", "from", "at",
    "note"}) when another team sent the case here. The card says who sent it and
    from where, and for an escalation or a send-back, why.

    `waiting` ({"question", "by", "at"}) is an open information request, and
    `history` every decision recorded so far.

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
    ]
    if po_number:
        blocks.append(_section(f"*Purchase order*  `{escape_mrkdwn(po_number)}`"))
    blocks.append(_section(f"*Routed to*  {TEAM[destination]}"))
    path = case_path(history)
    if path is not None:
        blocks.append(
            {"type": "context", "elements": [{"type": "mrkdwn", "text": f"*Path*  {path}"}]}
        )
    if handed_over is not None:
        blocks.append(_arrival(handed_over))
    if waiting is not None:
        blocks.append(
            _section(
                f"*Waiting for information:* {escape_mrkdwn(waiting['question'])} "
                f"(asked by <@{waiting['by']}>, {waiting['at']})",
                verbatim=True,
            )
        )

    if recommendation is not None:
        spans = "\n".join(
            f"> {escape_mrkdwn(span['text'])}  _({span['source'].replace('_', ' ').lower()})_"
            for span in recommendation["evidence"]
        )
        blocks.append(
            _section(
                f"*Wording check*  `{recommendation['recommendation']}`  "
                f"(confidence {recommendation['confidence']:.2f})\n"
                f"{escape_mrkdwn(recommendation['explanation'])}\n{spans}"
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

    controls = {
        "type": "actions",
        "block_id": f"triage:{run_id}",
        "elements": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": ACTION_LABELS[action]},
                "action_id": action.value,
                "value": run_id,
            }
            for action in (ACTIONS_FOR[destination] if actions is None else actions)
        ],
    }
    disclaimer = {"type": "context", "elements": [{"type": "mrkdwn", "text": DISCLAIMER}]}
    blocks.extend(_history_blocks(history, room=MAX_BLOCKS - len(blocks) - 2))
    blocks.extend([controls, disclaimer])
    return blocks


def build_decided_card(
    *,
    invoice_number: str,
    action: TriageAction,
    decided_by: str,
    decided_at: str,
    handed_to: TriageDestination | None = None,
    note: str | None = None,
    history: Sequence[dict] = (),
) -> list[dict]:
    """The card after a decision, replacing the buttons.

    For a hand-off it says where the case went, because the case continues on a
    new card in that team's channel: "Escalated by <person>: <reason>. It is now
    with escalations." For a final decision it says what was decided, and lists
    every decision before it with its reason or note: the case is closed.

    Best effort (ADR-007). The database is authoritative and this is a view; a
    lost response can leave it stale while the decision is correctly recorded.
    """
    line = decision_line(
        {"action": action, "by": decided_by, "at": decided_at, "note": note, "to": handed_to}
    )
    if handed_to is not None:
        line += f"\nIt is now with {TEAM[handed_to]}."
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"Bill {invoice_number} triaged"},
        },
        _section(line, verbatim=True),
    ]
    blocks.extend(_history_blocks(history, room=MAX_BLOCKS - len(blocks) - 1))
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": DISCLAIMER}]})
    return blocks


def build_note_modal(
    action: TriageAction,
    *,
    run_id: str,
    invoice_number: str,
    message_ts: str | None,
    channel: str | None,
    team: str = "",
) -> dict:
    """The modal a control that needs a note opens. Nothing is recorded until
    it is submitted, and cancelling it records nothing.

    `private_metadata` carries the run and the card clicked, because a
    submission arrives without either. It comes back in a signed request, and
    the submission is checked against the database exactly as a click is.
    """
    title, submit, question = MODAL_TEXT[action]
    return {
        "type": "modal",
        "callback_id": MODAL_CALLBACK,
        "private_metadata": json.dumps(
            {
                "run_id": run_id,
                "action": action.value,
                "message_ts": message_ts,
                "channel": channel,
            }
        ),
        "title": {"type": "plain_text", "text": title},
        "submit": {"type": "plain_text", "text": submit},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "context",
                "elements": [
                    {"type": "plain_text", "text": f"Bill {invoice_number}. {DISCLAIMER}"}
                ],
            },
            {
                "type": "input",
                "block_id": NOTE_BLOCK,
                "label": {"type": "plain_text", "text": question.format(team=team)},
                "element": {
                    "type": "plain_text_input",
                    "action_id": NOTE_BLOCK,
                    "multiline": True,
                    "min_length": 1,
                    "max_length": NOTE_LIMIT,
                },
            },
        ],
    }


def build_alert(
    *,
    workflow: str,
    run_id: str | None = None,
    failed_node: str | None,
    message: str | None,
    execution_id: str | None,
    failed_at: str | None,
) -> list[dict]:
    """A failure alert. Every value is escaped: an error message can quote
    supplier or provider text, and none of it may ping a channel."""
    lines = [f"*Workflow failed*  `{escape_mrkdwn(workflow)}`"]
    if run_id:
        lines.append(f"Run: `{escape_mrkdwn(run_id)}`")
    if failed_node:
        lines.append(f"Node: `{escape_mrkdwn(failed_node)}`")
    if message:
        lines.append(f"> {escape_mrkdwn(' '.join(message.split()))}")
    blocks = [_section("\n".join(lines))]
    detail = " | ".join(
        part
        for part in (f"execution {execution_id}" if execution_id else "", failed_at or "")
        if part
    )
    if detail:
        blocks.append(
            {"type": "context", "elements": [{"type": "mrkdwn", "text": escape_mrkdwn(detail)}]}
        )
    return blocks
