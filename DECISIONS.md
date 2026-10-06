# Architecture decisions

Decisions that shaped this system, with the reasoning and the alternatives
rejected. ADR-001 through ADR-005 were made for the full design, before the v1
scope was set, and are shown here with their v1 status. ADR-006 through ADR-009
are v1 decisions, recorded in `BUILD-SCOPE-v1.md`. ADR-010 is a v1.1 decision.

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
boundary (`docker/postgres/init/001-bootstrap.sh`, asserted by `make check-db`)
means that even a leaked n8n configuration
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
atomically with a transaction. The semantic attempt lifecycle
(`migrations/004_semantic.sql`, `policy_service/domain/semantic.py`) handles that
honestly: the attempt is recorded before the call and finalised after it.

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
not used: it would put a Slack token in n8n, which ADR-002 rules out.

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
It adds a second credential, and its cost position is unverified. v1 does not
need it.

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
outbox, leases, fencing, replay and `OUTCOME_UNKNOWN`: the deferred durable
outbox and recovery and replay components in full.

**Consequences.** Eleven invariants have no subject in v1 and are recorded as not
applicable with a reason in `docs/invariant-register-v1.md`. The triage loop needs
a visible ending in Slack instead, which is ADR-007. The reverse is cheap: the
repository tree is a strict subset of the full design's tree, so building the
durable outbox later is additive rather than a rewrite.

---

## ADR-007: the triage loop closes with a best-effort card update

**Status:** accepted, v1.

**Context.** With no Xero write, a person clicks a triage control and nothing
observable happens outside the database. The loop has no visible ending.

**Decision.** After the decision commits, update the Slack card in place to show
the decision, who made it, when, and the destination.

**This is best-effort and is not the durable `SLACK_RESULT_UPDATE` lifecycle the
deferred durable outbox would provide.** A lost response can leave the card stale while the decision is
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
shape. The mitigation is re-capturing them with `scripts/capture_fixtures.py`;
no response schema validates them yet.

---

## ADR-009: "Send to finance" and "Send to procurement" hand the case over

**Status:** accepted, v1.

**Context.** The AP review card offers "Send to finance" and "Send to
procurement". They were recorded as final decisions: the run closed, the card
updated in `#ap-review`, and nothing reached the other team. The control's name
promised a hand-off that did not happen, and a case "sent to finance" was closed
without finance seeing it, which breaks BR-6.

**Decision.** A hand-off is recorded as a decision that does not close the run.
The run moves to the new destination and back to `REVIEW_READY`, the AP card
says where the case went, and notify posts a new card in that team's channel
with that team's controls and a line saying who sent it. That team's decision
is the final one. The schema allows any number of hand-offs and exactly one
final decision per run (`005b_triage_handoff.sql`), and rows stay immutable, so
the record shows who handed the case over and who decided it.

Only the current card's controls count: the card on record must have been
posted, for the run's current team, and be the card clicked. So a click on the
card a hand-off replaced is refused, including in the gap before the new team's
card is recorded and after it fails to post, as is a control the current
destination does not offer.

**Consequences.** The interaction handler makes two Slack calls, the card update
and the new card, inside Slack's three-second window. If the new card fails to
post, the run waits in `REVIEW_READY`, and the poll re-sends it after 15 minutes;
the notify endpoint runs again for that run rather than replaying the earlier
card, because that card belongs to the team the case has left.

**Rejected alternative.** Post an information-only card to the other team and
keep the decision final. Simpler, with no schema change, but the receiving team
could not act on the card and the system would not record what they decided.

---

## ADR-010: escalation and information requests keep the case open

**Status:** accepted, v1.1.

**Context.** "Escalate" and "Request more information" were recorded as final
decisions. One click closed the run, nobody was told, and no reason or question
was captured. A case "escalated" reached no one, and a question "requested"
was asked of no one.

**Decision.** Both become decisions that do not close the run, using the
hand-off machinery of ADR-009, and both are made by a person.

- **Escalate** asks for a reason (1 to 500 characters) in a Slack modal and
  hands the case to `#ap-escalations` on a new card. The card shows the
  reason, who escalated and when, and the path the case has taken. The card it
  replaces says "Escalated by <person>: <reason>", and a click on it is refused
  as superseded. Escalate is offered where it was: finance, procurement and
  duplicate review, not AP review.
- **One escalation per run**, enforced in the service and by a unique index
  (`005c_escalation_and_information.sql`). There is no second level.
- **Send back**, on the escalations card, asks for a note and returns the case
  to the team that escalated it, on a new card showing the note. Escalate is
  not offered again. **Mark reviewed** on the escalations card closes the case.
- **Request more information** asks "What information is needed, and from
  whom?" and records the question on the current card, which stays where it
  is. While the question is open the card offers only "Information received",
  and Escalate where Escalate is offered. The answer, also typed into a modal,
  restores the card's controls, and both stay on the card. Any number of
  rounds, one open at a time. A question asked before an escalation is still
  open on the escalations card.
- The server checks every click and submission against the case's state, not
  only against the card, and refuses anything else with 409.
- **No timers, deadlines, reminders or automatic escalation.** Nothing moves a
  case except a person. Open question 4 in `docs/business-requirements.md` is
  still open.
- **No contact outside Slack.** Request more information notifies nobody: no
  email, no direct message, nothing to a supplier. The person asking is
  expected to ask.
- Routing never produces the escalations destination (I41). Exactly one final
  decision per run still holds.

**Consequences.** A modal submission is answered as soon as its decision
commits, and the card work (the update, and for a hand-off the new card) runs
after the acknowledgement. If it fails, a hand-off waits in `REVIEW_READY` and
the poll re-sends it after 15 minutes, as in ADR-009. A request or answer
redraws the card in place; if that update is lost (ADR-007), the next refused
click on the card redraws it with the controls that apply. A late redraw can
briefly restore buttons on a card just decided, and the next click on it
refreshes the card from the decision. Notes are stored, bounded, escaped on
every card and never sent to a model (I43).

**Rejected alternatives.** Keep Escalate final and only record a reason: the
case would still reach no one. Let the person pick who to escalate to, or
assign a named person: that needs a directory of people and roles this build
does not have. Have Request more information email the supplier: contact
outside Slack, and a write path the project rules out. An escalation timer:
open question 4, which needs a policy from a real stakeholder first.
