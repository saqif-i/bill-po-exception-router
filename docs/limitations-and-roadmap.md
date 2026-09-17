# Limitations and roadmap

What this system does not do, why, and what it would take. Written because a
portfolio project that lists only its features is asking to be taken on trust,
and because the design guide this was built from spends most of its length
refusing to claim things it had not verified.

Nothing here is an apology. Every item is a decision with a reason, or a known
gap with a cost attached.

**Scope note (I23).** This project makes no claim about any organisation's
internal architecture, systems, processes or roadmap, and reaches no formal
compliance conclusion. The accounts-payable scenario in
`docs/business-requirements.md` is synthetic and was written by the builder. All
data is synthetic and builder-created.

---

## 1. The system does not write to Xero

v1 reads, reconciles, recommends and records. It never mutates anything in the
accounting system.

The full design permits one runtime mutation: an informational history note on an
allow-listed bill. It was cut for the reasons in ADR-006, the short version being
that the available write scope is broader than the operation needs, the source
that would settle how the operation works was never read, and one honest durable
write costs an entire subsystem.

**What follows from this:** the triage decision closes in Slack, not in Xero. A
reviewer in the AP system sees no trace of the decision. In a real deployment that
would be the first thing to add, and the guide's Volumes 10 and 11 describe how.

---

## 2. Deferred subsystems

Five volumes of the original design are not built. Each is deferred, not
abandoned, and the repository tree is a strict subset of the canonical tree so
each can be added without rework.

| Deferred | What it provides | Why not in v1 |
|---|---|---|
| **Transactional outbox** (V10) | Durable intent for every external call, so a dead process loses no work | Its purpose is to make an external mutation honest. v1 makes none. |
| **Retries, recovery and replay** (V11) | `OUTCOME_UNKNOWN`, action-specific replay, dead-letter view, manual reconciliation of unknown write outcomes | Same dependency. There is no external write whose fate could be unknown. |
| **Fixture reset machinery** (V05) | Fixture identity surviving a Demo Company reset, under a durable leased operation | Fixtures are reseeded by hand after a reset. See section 4. |
| **Optional webhooks** (V13) | Event-driven ingestion instead of polling | Not a cut. Its own completion gate forbids implementation while S14b is unread, and S14b was never resolved. Recorded as `NOT_APPLICABLE`. |
| **Security and performance hardening depth** (V14) | Audit-grade privacy controls, load characterisation | Reduced to the controls that fit v1's surface, documented in `docs/security.md`. |

---

## 2b. A bill carries two identifiers in one Xero field

Xero returns the `Reference` field only for sales invoices. On a **bill**, the
field the UI labels "Reference" arrives from the API as `InvoiceNumber`, and
there is no second free-text field.

So there is nowhere to record the supplier's invoice number and the purchase
order separately, and this build packs both into that one field on a documented
convention:

```
INV-1001 PO-1001
```

`normalisation.split_bill_reference` splits them: anything matching `PO-` is the
purchase-order reference, and what remains is the supplier's number. A bill with
no `PO-` token is the `NO_PO_REFERENCE` case rather than an error.

**This is a constraint the provider imposed, not a design choice.** A production
system would use a Xero tracking category, a custom field, or Xero's native
copy-to-bill linkage rather than parsing one string into two identifiers. The
convention is fragile in an obvious way: a supplier whose own invoice number
happens to contain `PO-` would be misparsed.

It is recorded here because "I read the API, found the field I expected was not
there, and handled it deliberately" is the honest description, and because
anyone extending this should replace the convention rather than build on it.

---

## 3. Guarantees deliberately not claimed

**Slack delivery is at-least-once. Duplicates are possible.** Without a durable
posting intent, a post that succeeds with a lost response can produce a second
card. This is visible in the audit trail. Exactly-once Slack posting is not
achievable through the Slack API and the guide's own risk register says it must
not be claimed; v1 holds that line without the machinery that narrowed the window.

**The card update is best-effort.** A lost response can leave a card showing an
open exception whose decision was correctly recorded. The database is
authoritative and the card is a view. See ADR-007.

**No exactly-once processing of bills.** Re-polling an unchanged bill is safe
because of the application-level idempotency key, not because delivery is
guaranteed once.

**No escalation timer.** An exception routed to a destination sits there until a
human acts. Nothing chases it. This is open question 4 in
`docs/business-requirements.md` and it would change the design.

---

## 4. Operational limitations

**The Xero Demo Company resets after 28 days** and deletes everything added to it.
Fixtures must be recreated by hand afterwards. The recorded demonstration is the
durable artefact; the live system is not guaranteed to be runnable on any given
day without reseeding.

**The demonstration is single-operator.** The builder plays the supplier, the AP
officer and every downstream team. There is no real reviewer, no real workload and
no real queue depth.

**Single worker assumed.** v1 is not tested under concurrent workers, because the
generic lease and fencing machinery (I24) is deferred. Running two instances of
the polling path concurrently is not supported.

**No load characterisation.** The system has not been run at volume. The
assumption of roughly 400 bills per month in the requirements brief is an
assumption, not a measurement, and nothing here validates it.

---

## 5. Invariants with no subject in v1

The design defines forty operational invariants. v1 weakens none of them, but
eleven govern machinery it does not contain. Full reasoning and the status of all
forty are in `docs/invariant-register-v1.md`.

| Status | Count |
|---|---|
| Enforced | 23 |
| Enforced, reduced surface | 3 |
| Deferred | 3 |
| Not applicable | 11 |

**All eleven trace to one decision.** There is no Xero write, therefore no durable
action intent, no outbox, no command fence, no replay and no unknown outcome to
reconcile. They are I18, I21, I22, I25, I29, I30, I35, I37, I38, I39 and I40.

The three deferred (I24, I33, I34) are the generic lease and fencing rules. They
are deferred rather than not applicable because a later volume would introduce
leased operations that need them.

---

## 6. Sources still unread

The design guide tracks every external fact to a source with an access date, and
marks a source `PENDING_ACCESS` when the canonical page could not be read. Seven
remain pending. **None is blocking for v1**, and each is listed with what it would
settle.

| Source | Subject | What it would settle |
|---|---|---|
| S03 | Custom Connections guide | The exact scope options shown in the portal, and whether a narrower write scope now exists under granular scopes |
| S06 | Accounting API overview | Request conventions and pagination behaviour |
| S08 | Invoices operation | Summarisation, discount behaviour, unit decimal handling |
| S09 | Purchase Orders operation | Narrative status and retrieval guidance |
| S10 | History and Notes | History-note length and counting limits |
| S15 | Use the demo company | Reset cadence, manual reset steps, deletion behaviour |
| S33 | n8n concurrency | Current self-hosted workflow concurrency controls |

Two sources stopped being blocking under v1's scope rather than by being read:
**S10a** (whether `POST` is documented for history records) and **S14b**
(webhook configuration for Custom Connections). Both gated work that is not built.
They remain `PENDING_ACCESS`; a pending source is not promoted to verified because
a release is due.

**S15 is worth closing first.** The 28-day reset cadence in section 4 above comes
from Xero's support material read during planning rather than from the canonical
page on the recorded access date, so it is stated here as an operational fact and
tracked as an unread source until someone opens the page.

---

## 7. Roadmap, in the order it should be built

**1. Durable outbox (V10).** Reactivates I21, I22, I29, I35 and I40, and restores
I07 to its full surface. Prerequisite for everything below, and for any external
write. Read S10a first.

**2. Retries, recovery and replay (V11).** Reactivates I18, I25, I30, I37, I38 and
requires I24 and I39. Turns the best-effort card update into a durable lifecycle
and gives operators a dead-letter view.

**3. Fixture reset machinery (V05).** Reactivates I33 and I34. Removes the manual
reseeding tax after every Demo Company reset.

**4. Escalation and ageing.** Not in the original design. Open question 4 in the
requirements brief. Requires a policy from a real stakeholder before it can be
built.

**5. Webhooks (V13).** Only if S14b establishes that a Custom Connection can be
configured for webhook delivery. Current reading suggests webhook configuration is
a per-app facility that may not be available to this connection type, which is
exactly why the source needs reading rather than assuming.

**Not on the roadmap:** automated approval at any threshold. See section 6 of
`docs/business-requirements.md`. Relaxing that boundary stops this from being a
triage tool and makes it an unaccountable approver.

---

## 8. What would need to change for real use

Beyond the roadmap above, a real deployment would need a stakeholder-agreed
tolerance policy, a routing table validated by the teams receiving the
exceptions, a retention period for the decision record, a second pair of eyes on
duplicate closure, and a security review of the credential model against the
organisation's own standards. None of those is an engineering problem, and none
can be answered by the builder alone. They are listed as open questions 1 through
6 in `docs/business-requirements.md`.
