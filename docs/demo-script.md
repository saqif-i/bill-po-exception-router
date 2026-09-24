# Demonstration script

Under three minutes, and it must be watchable without a live system. Record it
once the pipeline works; a recording is the durable artefact because the Xero
demo company resets after 28 days.

## Shot list

| Time | Show | Say |
|---|---|---|
| 0:00 | Two bills side by side in Xero, `INV-1008` and `INV-1009` | "A supplier has billed for bottled water. The wording on both bills differs from the purchase order." |
| 0:15 | n8n, press Execute Workflow | "A schedule polls for draft bills. Nothing here is manual." |
| 0:30 | The `runs` query output | "Nine bills. One matched and closed silently. The rest name a specific problem and a team that owns it." |
| 0:50 | `#ap-procurement`, the quantity variance card | "This one is arithmetic. Eleven billed against ten ordered. No AI was involved, and none was needed." |
| 1:10 | `#ap-review`, the `INV-1008` card | "This one is wording. Every number agrees, so the system asked a model whether the two descriptions mean the same thing. It answered, with the supplier's own words as evidence." |
| 1:30 | The line on the card saying it is context, not a decision | "It is context. It changes no outcome, it is not a routing input, and a person still decides." |
| 1:45 | The `INV-1009` card, no recommendation | "Same wording difference. But the unit price is five dollars higher, so the model was never asked. The reason is recorded." |
| 2:05 | Click a triage control | "The controls are triage, not approval. Nothing here approves, rejects or pays anything." |
| 2:15 | The `triage_decisions` row | "The decision is recorded with what the person was shown at the time: the exceptions, the recommendation, and why there was or was not one." |
| 2:35 | The evaluation output | "And the gate is measured, not asserted." |

## The two-card contrast is the whole thing

If you cut everything else, keep `INV-1008` beside `INV-1009`. Identical wording
difference, one consulted a model, one refused to and said why. That is the
project in fifteen seconds.

## Before recording

- Reseed if the demo company has reset
- `make gate` passes
- No `.env` open in a visible window, no terminal with a token in scrollback
- Slack shows the four channels and nothing else
- Run `python scripts/capture_metrics.py` and screenshot it

## Rules

**Show the refusal, not just the success.** Anyone can demonstrate an AI
answering. Demonstrating one being correctly refused is rarer and more
persuasive.

**Do not narrate internals.** No fencing tokens, no idempotency registry. If
someone wants that, the repository is there.

**Claim only measured numbers.** If you have not run the
evaluation, say nothing about accuracy.
