# Build scope, v1

**Status:** governing document for the v1 build.

v1 is a deliberately reduced build of a larger design. This document records what
v1 builds, what it defers, and why. Section 2 names every deferred component, and
`docs/limitations-and-roadmap.md` describes each one and the order in which it
should be built.

Nothing in this document weakens an operational invariant. See
`docs/invariant-register-v1.md` for the status of all forty.

---

## 1. The decision that shapes everything else

**v1 performs no external mutation.** It reads from Xero, reconciles, asks for a
bounded recommendation on genuinely ambiguous wording, routes to a human, and
records the decision. It never writes to Xero.

The full design permits exactly one runtime mutation: an informational history
note on an allow-listed bill (I07). Three things argue against building it in v1.

**The write surface is broader than the write.** This is a known risk: the
invoice write scope is wider than a history note needs, and no history-note-only
scope has been established. Taking a scope broader than the operation requires,
on a project whose central claim is that nothing changes a bill, is the wrong
trade.

**A blocking source was never resolved.** S10a, the Xero history and notes
documentation, is `PENDING_ACCESS` and is marked blocking before stating either
that `POST` is supported or that it is not. The project's rule is not to infer
missing provider details. Cutting the write removes the dependency rather than
guessing at it.

**One durable external write costs a subsystem.** Doing it honestly requires the
transactional outbox, leases, fencing, replay and `OUTCOME_UNKNOWN`: the durable
outbox and the recovery and replay components in full. That is the largest
single body of work in the design.

The consequence is that eleven invariants have no subject in v1. They are
recorded as not applicable with a reason, not quietly dropped.

---

## 2. What v1 builds

| Area | Scope in v1 |
|---|---|
| Repository foundation and CI | Full. Pinned dependencies, secret scanning, CI, two-database boundary. |
| FastAPI service foundation | Reduced. Auth, correlation IDs, safe errors, health and readiness, request limits, the `Idempotency-Key` contract. No outbox, retention, status or webhook endpoints. |
| PostgreSQL data model | Reduced. Four migrations: `001_core_schema.sql` (runs, bills, purchase orders, fixtures, ingestion, idempotency registry), `003_reconciliation.sql`, `004_semantic.sql` and `005_triage_and_slack.sql` reduced to the triage and decision tables. No lease or fencing machinery. |
| Xero connection and read client | Read path only. Custom Connection, token caching, timeouts, bounded retries, `Retry-After` handling, decimal-safe parsing. The transport allow-list contains no write method. |
| Deterministic reconciliation | Full. This is the core and is not reduced. |
| n8n polling and orchestration | Three workflows: polling, processing, error handling. |
| Claude semantic recommendation | Full, including the invocation gate, both schemas, prompt-injection resistance and the evaluation harness. This is the differentiator and is not reduced. |
| Slack human triage | Reduced. Verified ingress, decision capture, audit trail, and a best-effort in-place card update showing the decision, who made it, when and the destination. No durable result-update lifecycle. |
| Evaluation and demonstration | The evaluation harness, measured results in the README, and a recorded demonstration. |

**Not built:**

| Component | What it would add |
|---|---|
| Durable outbox | A durable intent for every external call, the Xero history-note write, and the durable Slack result-update lifecycle. |
| Recovery and replay | Workers with leases and fencing, `OUTCOME_UNKNOWN`, action-specific manual replay, a dead-letter view. |
| Fixture reset machinery | Fixture identity that survives a Demo Company reset, under a durable leased operation. |
| Webhooks | Event-driven ingestion instead of polling. |
| Hardening depth | Audit-grade privacy controls and load characterisation. |

Webhooks are not a cut. Implementation is forbidden while S14b is unread, and
S14b was never resolved. The component is `NOT_APPLICABLE`.

---

## 3. Guarantees v1 makes, and the ones it declines to make

The design's strongest quality is that it refuses to claim guarantees it cannot
deliver. v1 keeps that standard with less machinery.

**Slack delivery is at-least-once, and duplicates are possible.** Without a
transactional outbox there is no durable posting intent, so there is no marker
reconciliation and no `POSSIBLE_DUPLICATE` state backed by one. A post that
succeeds with a lost response can produce a second card. This is visible in the
audit trail and is not claimed to be exactly-once. Exactly-once Slack posting is
not achievable through the Slack API and must not be claimed; v1 holds that line
without the machinery that would narrow the window.

**The triage loop closes in Slack, not in Xero.** With no external write, the
card is updated in place after a decision so the loop has a visible ending. That
update is best-effort and is covered by the at-least-once position above. It is
not the durable `SLACK_RESULT_UPDATE` lifecycle the durable outbox would provide,
and it makes no claim to be.

**Demo Company resets are an operational limitation, not a solved problem.** The
Xero Demo Company resets after 28 days and deletes everything added to it. The
deferred fixture reset machinery would let fixture identity survive a reset. v1
does not. Fixtures are reseeded after a reset, and the recorded demonstration is
the durable artefact.

**No AI output reaches an outcome.** The model is invoked only on a clean
residual pair, never produces `MATCHED`, never alters `reconciliation_outcome`,
and every reviewed run reaches a human. Missing, malformed, refused, truncated or
low-confidence output routes to review with no recommendation displayed.

**No claim is made about any organisation's internal systems.** No compliance
conclusion is offered. All data is synthetic and builder-created.

---

## 4. Repository scope rules

**The v1 tree is a strict subset of the full design's tree.** No path is renamed,
relocated or repurposed. Every deferred module keeps its reserved name so that
building the durable outbox or recovery and replay later is additive.

Four consequences that are easy to get wrong:

1. **Migration numbering has one gap.** v1 creates `001`, `003`, `004` and
   `005`, the last reduced to the triage and decision tables without the Slack
   result-update lifecycle that belongs to the durable outbox. `002` is reserved
   for the fixture reset machinery, and `006` through `008` for the durable
   outbox, recovery and replay, and webhooks. `db/migrate.py` applies files in
   lexical order of what exists on disk and must not assert a contiguous
   sequence. Do not renumber and do not reuse `002`.
2. **`xero_transport.py` stays separate from `xero_client.py`** even though v1 has
   no write path, so the method-and-path allow-list stays testable without
   constructing a client.
3. **`demo_seed/` remains a sibling of `policy_service/`, not a subpackage**, so
   seeding stays a separate program with separate credential handling (I07).
4. **`tests/live/` is retained** with its pytest marker and CI exclusion, even
   though v1 has few live tests. I17 requires the mechanism to exist, not merely
   that no live test happens to run.

---

## 5. Effect on the pending sources

Every external provider fact the design relies on is tied to a source with an
access date. Nine sources were `PENDING_ACCESS`. Two stop being blocking under
this scope, because the work they gated is not built:

| Source | Was | Now |
|---|---|---|
| S10a, Xero history and notes | Blocking before any statement about `POST` support | Not blocking. v1 writes no history note and makes no claim either way. |
| S14b, Xero webhooks overview | Blocking before any webhook implementation | Not blocking. Webhooks are `NOT_APPLICABLE`. |

Seven pending sources remain, none blocking for v1. They are listed in
`docs/limitations-and-roadmap.md`.

---

## 6. Definition of done for v1

1. A seeded bill with a genuine exception travels from the n8n schedule to a
   recorded human decision, end to end, without manual intervention between hops.
2. Deterministic reconciliation resolves the large majority of exceptions, and
   the measured invocation rate for the model is recorded in
   the [README results](README.md#results).
3. The model is invoked only when the residual pair is clean and wording is the
   sole unresolved difference, proven by a test that the account-code gate set is
   excluded before any model call.
4. Every failure mode in `docs/runbook.md` has been triggered deliberately at
   least once and behaved as documented.
5. No secret appears in any log, fixture, workflow export, screenshot, evaluation
   file or commit.
6. `docs/limitations-and-roadmap.md` lists every deferred component and every
   not-applicable invariant with its reason.
7. A demonstration under three minutes exists and does not require a live system
   to be watchable.

Items 4, 5 and 6 are not optional under time pressure. If time runs short, reduce
scope in section 2, not in this list.

---

## 7. What reactivates the deferred work

Building the durable outbox restores I07 to its full surface and reactivates I21,
I22, I29, I35 and I40. Building recovery and replay reactivates I18, I25, I30,
I37 and I38 and requires I24 and I39. Building the fixture reset machinery
reactivates I33 and I34. Webhooks require S14b to be read first.

None of that is blocked by v1. The tree, the migration numbering and the
invariant register are all arranged so the deferred components can be built later
without rework.
