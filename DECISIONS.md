# Architecture decisions

Decisions that shaped this system, with the reasoning and the alternatives
rejected. ADR-001 through ADR-005 come from the implementation guide (Volume 00
section 7) and are reproduced here with their v1 status. ADR-006 through ADR-008
are v1 decisions, recorded in `BUILD-SCOPE-v1.md`.

An ADR is not amended silently. Where v1 changed one, the original position is
stated first and the change is marked.

---

## ADR-001: PostgreSQL is the only infrastructure dependency

**Status:** accepted, reduced in v1.

**Context.** The system needs durable state, scheduling, and a way for a worker
to take work without two workers taking the same item. The obvious reach is for a
queue, a cache, and a scheduler.

**Decision.** PostgreSQL and nothing else. Locking is `FOR UPDATE SKIP LOCKED`.
Scheduling is n8n. Every additional service is another credential to hold,
another failure mode to handle and another thing to explain.

**v1 note.** The original wording says durable queuing is an outbox table. v1
builds no outbox, because it performs no external mutation (ADR-006). The
decision stands in the form that matters: no Redis, no Celery, no Kafka, no
vector database, no second message broker.

**Consequences.** Fewer moving parts, and a demonstration that runs from one
`docker compose up`. The cost is that a genuinely high-throughput version would
outgrow this, which is the right trade for an internal automation workload
measured in hundreds of documents per month.

---

## ADR-002: n8n holds one secret and never reaches the application database

**Status:** accepted, unchanged in v1.

**Context.** n8n is a workflow tool with a visual editor and an execution log. A
credential placed in it is visible to anyone with editor access and can be
exported in a workflow definition.

**Decision.** n8n holds exactly one secret, the bearer token for the policy
service. It has no database credential, no Xero credential, no Anthropic key and
no Slack token. It calls the service over HTTP and nothing else.

**Enforced by database role grants, not only by configuration.** The two-database
boundary from Volume 01 section 9.9 means that even a leaked n8n configuration
cannot read application data.

**Consequences.** Business logic must live in the service rather than in workflow
nodes, which is the right default here but is worth naming: in an iPaaS shop the
instinct runs the other way, and `docs/platform-mapping.md` addresses where that
line should sit.

---

## ADR-003: Claude is called by the policy service, not by n8n

**Status:** accepted, unchanged in v1.

**Context.** n8n has an HTTP node. Calling the Anthropic API from a workflow would
work and would be visible in the execution log.

**Decision.** The call is made from `policy_service/integrations/claude_client.py`.

Three reasons. Strict schema validation and evidence substring checking run in
tested in-process code before anything is persisted as usable. The attempt-start
record is committed before the call, so an unrecorded call is detectable. The
prompt lives in a versioned file rather than a workflow canvas field, so a prompt
change is a reviewable diff.

**This is not an atomicity argument.** A remote call cannot be committed
atomically with a transaction. Volume 08 defines the multi-step lifecycle that
handles that honestly, and v1 implements it.

---

## ADR-004: a single Slack client, in the policy service

**Status:** accepted, amended for v1.

**Context.** The card could be posted by n8n. The credential model permits it.

**Decision.** One Slack client, in the service. The service must hold the signing
secret to verify inbound interactions, so splitting posting from receiving would
put Slack credentials in two places for no gain.

**v1 amendment.** The original says `chat.postMessage` and `chat.update` are
called from the outbox worker. v1 has no outbox worker. Both calls are made
directly: the post from the notify endpoint, the update from the interaction
handler after the decision commits. This makes the update best-effort rather than
durable, which is stated in `BUILD-SCOPE-v1.md` section 3 and is not claimed to be
otherwise.

**Rejected alternative.** n8n posts the card. Permitted by the credential model,
not used. Volume 15 records the trade-off.

---

## ADR-005: the demonstration supplier contact is created by hand

**Status:** accepted, strengthened in v1.

**Context.** Seeding needs a supplier contact. Creating one requires a write
scope on contacts.

**Decision.** Create it by hand in the Xero UI. The requested contacts scope is
read-only, so a seed command cannot create contacts, and broadening the runtime
scope to allow it would defeat least privilege.

**v1 strengthening.** v1 extends the same reasoning to everything else. All
fixtures, including purchase orders and bills, are created by hand in the Demo
Company. This removes the last argument for any write scope at runtime, so the
connection is read-only and the claim that nothing changes a bill is backed by the
credential itself rather than by application logic.

**Rejected alternative.** A second, seed-only connection with write scopes.
Volume 05 records this and requires its cost position to be recorded as verified
or unverified rather than assumed. v1 does not need it.

---

## ADR-006: v1 performs no external mutation

**Status:** accepted, v1.

**Context.** The full design permits exactly one runtime mutation: an
informational history note on an allow-listed bill (I07).

**Decision.** v1 writes nothing to Xero. No write method exists on the transport
allow-list.

**Three reasons.** The available write scope is broader than a history note needs,
and no history-note-only scope has been established, which is a poor trade on a
project whose central claim is that nothing changes a bill. S10a, the source that
would settle whether `POST` is supported for history records, is still
`PENDING_ACCESS`, and the project's rule is not to infer missing provider details.
And one durable external write honestly implemented requires the transactional
outbox, leases, fencing, replay and `OUTCOME_UNKNOWN`, which is Volumes 10 and 11
in full.

**Consequences.** Eleven invariants have no subject in v1 and are recorded as not
applicable with a reason in `docs/invariant-register-v1.md`. The triage loop needs
a visible ending in Slack instead, which is ADR-007. The reverse is cheap: the
repository tree is a strict subset of the canonical tree, so building Volume 10
later is additive rather than a rewrite.

---

## ADR-007: the triage loop closes with a best-effort card update

**Status:** accepted, v1.

**Context.** With no Xero write, a person clicks a triage control and nothing
observable happens outside the database. The loop has no visible ending.

**Decision.** After the decision commits, update the Slack card in place to show
the decision, who made it, when, and the destination.

**This is best-effort and is not the durable `SLACK_RESULT_UPDATE` lifecycle from
Volume 10.** A lost response can leave the card stale while the decision is
correctly recorded. The database is authoritative; the card is a view. This is
consistent with the at-least-once position already stated for Slack delivery, and
it is not claimed to be exactly-once.

**Rejected alternative.** Post a second message instead of updating. Cheaper to
reason about, but it leaves the original card showing an open exception that has
been resolved, which is worse than a stale update.

---

## ADR-008: develop against captured Xero responses, not the live API

**Status:** accepted, v1.

**Context.** The Demo Company resets after 28 days, access tokens last 30
minutes, and every live call is slower and less repeatable than reading a file.
Reconciliation takes two JSON documents and does not care where they came from.

**Decision.** Connect to live Xero on day 1, with a three-hour timebox, and save
the real API responses to `tests/fixtures/bills/` and
`tests/fixtures/purchase_orders/`. Develop and test against those files for the
rest of the build. Touch the live API again only to prove the polling path and to
record the demonstration.

**If the timebox is exceeded**, fall back to hand-written fixtures of the same
shape and record that in `docs/limitations-and-roadmap.md`. Everything downstream
is identical either way; what is lost is the ability to say the fixtures came from
the real API.

**Consequences.** Tests run offline, fast and deterministically, with no rate
limits and no token expiry. The risk is fixtures drifting from the real API
shape, mitigated by the response schemas from Volume 04 section 9.9 being
validated against both.
