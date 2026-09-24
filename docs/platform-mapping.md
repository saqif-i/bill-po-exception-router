# If this were built on SnapLogic or Tray

This project uses n8n. A team running an enterprise iPaaS would build it
differently, and the interesting question is not "could it be ported" but
**which parts belong in the platform and which would still be code**.

Written because that judgement is the job, and because a portfolio piece that
cannot say where its own boundaries should move has not been thought about
hard enough.

## The vocabulary, so the mapping is legible

| n8n | SnapLogic | Tray |
|---|---|---|
| Workflow | Pipeline | Workflow |
| Node | Snap, grouped into Snap Packs | Connector step |
| Self-hosted runtime | Snaplex, the execution plane | Hosted |
| HTTP Request node | HTTP Snap, or a purpose-built Snap | HTTP connector, or a custom connector |
| Error workflow | Pipeline error handling | Error handling branch |

SnapLogic leans toward data integration at scale with a large library of
versioned Snaps. Tray leans toward workflow orchestration with room for custom
connectors. Both handle auth, pagination, retries and schema in the connector
rather than in your code, which is the main thing that changes below.

## What would move into the platform

**The Xero read client, almost entirely.** `xero_auth.py`, `xero_transport.py`,
`xero_errors.py` and most of `xero_client.py` exist because n8n has no Xero
connector worth the name. A supported connector handles the client-credentials
grant, token caching, pagination, rate limiting and `Retry-After` for you, and
handles them better than a client one person wrote in an afternoon.

That is roughly 400 lines gone, and it is the correct trade.

**The Slack posting path.** `slack_client.py` becomes a connector step. The
inbound interaction endpoint is less clear cut; see below.

**Scheduling, branching and the error path.** Already in n8n here, and they
translate directly.

**Observability.** Correlation, execution history and alerting are platform
features rather than something to build.

## What would stay as code, and why

**The reconciliation engine.** All of `policy_service/domain/`. Fifty-five
tests that run in under a second with no network, a fixed tolerance set, an
exhaustive code vocabulary, and pairing logic with a precedence that matters.
Expressing that as connector steps would make it slower to test, harder to
review and impossible to reason about as a unit. It is business logic, not
integration.

**The semantic gate.** Twelve conditions, evaluated together, that decide
whether a model may be consulted at all. The gate's value is that it is one
function with one test suite. Spread across a canvas it becomes a diagram
nobody can verify.

**The response contract and its validation.** The whole safety argument is that
the model has no field through which it could affect an outcome. That argument
survives only if the schema and its server-side validation live somewhere
reviewable in a pull request.

**The idempotency registry.** Platforms offer idempotency helpers, and they are
usually about not re-running a step. This registry is about a contract with six
documented behaviours including atomic reclamation of a retryable failure. That
is domain logic wearing an infrastructure hat.

**Signature verification for inbound Slack.** A connector may offer it. If it
does, use it. If it offers only a generic webhook trigger, keep the verification
in code, because the timestamp window and the constant-time comparison are the
sort of thing that is subtly wrong when improvised.

## What I would lose

**Determinism of the deployed artefact.** This repository pins images by digest
and dependencies by hash. A hosted platform upgrades its own runtime, and a
connector version can change what your integration does without a commit in your
repository. That is a real trade, and usually worth it.

**Offline testing.** The fifty-five engine tests run with no network because
the engine is a pure function. Logic living in a canvas generally needs the
platform to test, which lengthens the loop from one second to minutes.

**Reviewability.** A pull request diff on a pipeline export is not a pull
request diff on a function.

## What I would gain

Connector maintenance someone else does. Retry, pagination and rate-limit
handling that has been used by many more people than this has. Promotion from
dev to test to prod, role-based access, and an operations view with alerting.
And the thing that matters most on a small team: **a colleague who does not
write Python can still change the flow.**

## The rule I would apply

Put it in the platform when it is **movement**: fetching, scheduling, routing
between systems, retrying, alerting.

Keep it in code when it is **judgement**: what counts as a variance, when a
model may be consulted, what a decision record must contain, what a failure
means.

By that rule this project is currently on the wrong side of the line for the
Xero client and on the right side for everything in `domain/`. On an iPaaS I
would delete the client, keep the engine, and expect the repository to shrink by
about a third.

