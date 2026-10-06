# Architecture

What exists, in the order a bill passes through it.

## The path

```
n8n schedule, every few minutes
  └─ GET Invoices from Xero, since the watermark
      └─ POST /runs/poll
            ├─ fixture allow-list check   -> not listed, no run is created
            ├─ ingestion version key      -> unchanged, no second run
            └─ a run, in INGESTED
                 └─ POST /runs/{id}/reconcile
                       ├─ eligibility                -> UNPROCESSABLE, closed
                       ├─ duplicate keys
                       ├─ purchase-order eligibility
                       ├─ header checks
                       ├─ pairing, three tiers
                       ├─ per-pair checks
                       ├─ header monetary checks
                       ├─ residual-pair comparison
                       └─ outcome, routing, gate result   [one transaction]
                            ├─ MATCHED        -> COMPLETED, silence
                            └─ REVIEW_REQUIRED -> REVIEW_READY
                                 ├─ POST /runs/{id}/semantic-review  [if gated open]
                                 │     └─ one attempt, recorded before the call
                                 └─ POST /runs/{id}/notify
                                       └─ Slack card -> AWAITING_TRIAGE
                                            └─ POST /slack/interactions
                                                  └─ decision  [one transaction]
                                                       ├─ final     -> COMPLETED
                                                       ├─ hand-off  -> REVIEW_READY,
                                                       │  new destination, new card
                                                       │  (send to a team, escalate,
                                                       │   send back)
                                                       └─ request or answer
                                                          -> same card, redrawn
```

## Components

| Component | Responsibility |
|---|---|
| n8n | Scheduling, branching, calling the service, error handling. Holds one secret |
| `policy_service/api/` | HTTP surface: auth, idempotency, limits, safe errors |
| `policy_service/domain/` | The decisions. Pure functions, no I/O |
| `policy_service/integrations/` | Xero, Anthropic and Slack clients |
| `policy_service/db/` | Migrations, the connection pool, the atomic writes |
| PostgreSQL | State, and several invariants enforced as constraints |

## Three structural decisions

**The engine is pure.** `reconcile()` takes a bill, a purchase order and a chart
of accounts, and returns a decision. No database, no network, no clock. That is
why the engine tests run in under a second and why the logic can be reviewed as
a unit.

**Several invariants live in the schema.** A run cannot be observable with an
outcome set while still reconciling. `integration_events` is append-only by
trigger and by privilege. `triage_decisions` is immutable. These hold even if
application code is wrong.

**Two databases, one boundary.** n8n has its own database and is refused on the
application's, asserted by `make check-db` in CI on every push.

## States

`INGESTED` → `RECONCILING` → then by outcome:

- `MATCHED` or `UNPROCESSABLE` → `COMPLETED`, no human, no Slack
- `REVIEW_REQUIRED` → `REVIEW_READY` → `NOTIFY_PENDING` → `AWAITING_TRIAGE` → `COMPLETED`

`REVIEW_READY` exists to resolve a real contradiction: the reconciler writes the
outcome, a review case receives `REVIEW_REQUIRED`, and `RECONCILING` requires a
null outcome. `semantic_stage_status` records progress through the model stage
rather than a second workflow status.

`NOTIFY_PENDING` lasts only while the card is being posted. Notify locks the run,
checks it, claims it and commits before calling Slack, so no lock or transaction
spans the call, and a second notify is refused by the claim. A post that does
not succeed returns the run to `REVIEW_READY` for a retry.

"Send to finance" and "Send to procurement" are hand-offs (ADR-009). The run
returns to `REVIEW_READY` with the new destination, notify posts that team's
card, and that team's decision closes the run. One final decision per run; any
number of hand-offs before it.

Escalate and Send back are hand-offs too (ADR-010). Escalate asks for a reason
in a Slack modal and moves the case to the escalations destination, which
routing never produces; Send back returns it to the team that escalated it.
One escalation per run. Request more information and Information received
also ask for a note, but keep the run where it is: the card is redrawn, and
while a question is open it offers only the answer, and Escalate where that is
offered. The controls a card offers and the clicks the server accepts come from
the same function, `allowed_actions()` in `policy_service/domain/triage.py`.

## What is not here

No transactional outbox, no replay, no retention process, no webhooks, and no
write to Xero. Those are deferred, with reasons, in
[`limitations-and-roadmap.md`](limitations-and-roadmap.md).
