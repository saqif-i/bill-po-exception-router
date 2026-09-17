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
                                                       -> COMPLETED
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
why fifty-five tests run in under a second and why the logic can be reviewed as
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
- `REVIEW_REQUIRED` → `REVIEW_READY` → `AWAITING_TRIAGE` → `COMPLETED`

`REVIEW_READY` exists to resolve a real contradiction: the reconciler writes the
outcome, a review case receives `REVIEW_REQUIRED`, and `RECONCILING` requires a
null outcome. `semantic_stage_status` records progress through the model stage
rather than a second workflow status.

## What is not here

No transactional outbox, no replay, no retention process, no webhooks, and no
write to Xero. Those are deferred, with reasons, in
[`limitations-and-roadmap.md`](limitations-and-roadmap.md).
