# Business requirements


## Provenance, stated plainly

This brief describes a **synthetic scenario**, written by the builder. It is not
an account of any real organisation's accounts-payable process, and no real
stakeholder was interviewed. Every figure in section 3 is an assumption, labelled
as one, chosen to be plausible rather than measured.

It exists because the rest of this project is engineering documentation, and a
system that automates a business process should be able to state what that
process is, who suffers from it, and what "better" means in terms the business
would recognise. Where this brief and the technical volumes disagree about what
the system should do, this brief describes the intent and the volumes describe
the mechanism.

This satisfies I23: no claim is made about any organisation's internal
architecture, systems, processes or roadmap.

---

## 1. The problem in one paragraph

An accounts-payable team receives supplier bills into Xero. Most should
correspond to a purchase order raised earlier by procurement. Before a bill is
paid, someone checks that what the supplier billed matches what was ordered. When
they do not match, the discrepancy has to be understood, routed to whoever can
resolve it, and tracked until it is closed. That checking is done by hand, one
bill at a time, and the volume grows with the business while the team does not.

The cost is not the matching. Most bills match, and confirming a match is quick.
The cost is the exceptions: finding them, working out which kind of problem each
one is, deciding who owns it, and making sure none is quietly forgotten.

---

## 2. Who is involved

| Role | Relationship to the process |
|---|---|
| **AP officer** | Opens each bill, compares it against the purchase order, decides whether it can proceed. Does this for every bill, every day. Primary user of anything built here. |
| **Procurement** | Owns the purchase order. Resolves quantity, price and line-count disagreements with the supplier. Receives exceptions, usually by email or a tap on the shoulder. |
| **Finance** | Owns the chart of accounts and tax treatment. Resolves coding and tax-type problems. Also receives exceptions informally. |
| **AP team lead** | Accountable for nothing being missed and for month-end closing on time. Has no reliable view of what is currently stuck or with whom. |

Nobody in this list wants a new system. They want the exceptions to arrive with
the right person, with enough context to act on, and to stop reappearing.

---

## 3. Current state, as assumed

Every figure below is an assumption, not a measurement. They are recorded so that
the success measures in section 7 have something to move against, and so that a
reader can see which claims would need validating with a real team.

| Assumption | Value | Why it matters |
|---|---|---|
| A1 | ~400 supplier bills per month | Sets the scale. Small enough for manual work to survive, large enough for it to hurt. |
| A2 | ~15% of bills carry at least one discrepancy | Roughly 60 exceptions per month. |
| A3 | ~3 minutes to confirm a clean match | ~17 hours per month spent confirming things that were already fine. |
| A4 | ~12 minutes to triage an exception | ~12 hours per month, and the variance is much wider than the mean. |
| A5 | Exceptions are routed by email or in person | No queue, no status, no record of who was asked. |
| A6 | Roughly one exception per month is discovered late | Usually at month-end, usually by the team lead, usually painfully. |

**The honest reading of these numbers:** the time cost is real but not dramatic.
The reason to automate is not the 29 hours. It is A5 and A6, the absence of a
record and the exceptions that fall through.

---

## 4. What goes wrong today

**Nothing distinguishes kinds of discrepancy.** A supplier billing 11 units
instead of 10 and a supplier using a description that does not obviously match
the ordered line are both "this doesn't match" in an email. They need different
people and different responses.

**Routing is informal, so ownership is unclear.** An exception sent to
procurement and an exception sent to finance look identical afterwards, which is
to say invisible.

**There is no record of the decision.** Six weeks later, nobody can say why a
particular bill was allowed through, who decided, or on what basis.

**Judgement calls consume the most time and produce the least record.** Deciding
whether "24x 500ml bottled water, assorted" and "Water, bottled, case of 24" are
the same thing takes a human ten seconds and leaves no trace. Deciding it wrongly
is not detectable afterwards.

**Duplicates are caught by memory.** The same invoice arriving twice is caught
because someone recognises it.

---

## 5. What the business is asking for

Stated as the AP team would state it, then translated.

> **"Tell me which bills need me, and why."**

**BR-1. Every bill is checked automatically against its purchase order, and only
genuine exceptions reach a person.** Clean matches are recorded and closed
without human involvement.

> **"When something needs me, tell me what kind of problem it is."**

**BR-2. Every exception carries a specific, named reason.** Not "mismatch" but
quantity variance, unit-price variance, line-amount variance, tax-type mismatch,
missing tax type, account-code mismatch, line-count mismatch, currency mismatch,
missing purchase-order reference, purchase order not found, duplicate invoice
number.

> **"Send it to the person who can actually fix it."**

**BR-3. Every exception is routed to a destination determined by its reason, not
by whoever is nearest.** Four destinations: finance, procurement, AP review,
duplicate review.

> **"Some of these are just wording. Don't make me read both documents to work
> that out."**

**BR-4. Where a bill line and a purchase-order line agree on every number and
differ only in wording, the system may offer an opinion on whether they describe
the same thing, with its evidence.** It offers; it does not decide.

> **"I need to be able to show what happened."**

**BR-5. Every decision is recorded with who made it, when, what they were shown
at the time, and what the system recommended if anything.**

> **"Don't let anything sit there silently."**

**BR-6. Nothing enters a state where it is neither resolved nor visible.** Every
failure produces something a person can see.

---

## 6. The boundary, and the conversation behind it

The obvious follow-up question from a stakeholder is:

> **"If it's confident, why can't it just approve the small ones? Under fifty
> dollars, say."**

The answer is no, and the reason is not technical caution.

**Approval is a financial control, not a task.** The person approving a payment
is accepting responsibility for it. That responsibility cannot be delegated to
software that cannot be held accountable, and a threshold does not change the
nature of the act, only its size.

**Confidence is not the same as being right.** A model asked whether two
descriptions match will produce an answer whether or not the text supports one.
Under a threshold, the errors it makes are exactly the ones nobody checks.

**Removing the human removes the record.** BR-5 exists because the current
process has no record. Automating approval would reintroduce that gap with better
formatting.

The system therefore has a fixed authority boundary:

| Layer | May decide | May never decide |
|---|---|---|
| **Deterministic rules** | Whether numbers, codes and identifiers agree. Whether a bill is a duplicate. Which destination an exception belongs to. | Whether ambiguous wording refers to the same item. |
| **Model** | Nothing. It produces a recommendation on wording, with evidence, or produces nothing. | Any outcome, any routing, any status. It never produces a match. |
| **Human** | The operational triage decision. | Nothing here approves, rejects or pays a bill. That happens in Xero, by a person, outside this system. |

The Slack controls are named for what they are: mark reviewed, send to finance,
send to procurement, request more information, escalate, close as duplicate. None
is named approve or reject, because none of them is.

**This boundary is the requirement most likely to be questioned and the one least
open to negotiation.** If it is relaxed, the system stops being a triage tool and
becomes an unaccountable approver.

---

## 7. How success is measured

| Measure | Target | Why this one |
|---|---|---|
| **M1** Exceptions resolved without a model call | High, and measured | If deterministic rules handle most of it, the model is doing a small, well-defined job rather than carrying the system. |
| **M2** Model invocation rate | Low, and measured | A rising rate means the deterministic layer is losing ground. |
| **M3** Exceptions with a named reason | 100% | BR-2. An unnamed exception is the old process with extra steps. |
| **M4** Decisions with a complete audit record | 100% | BR-5. |
| **M5** Exceptions with no visible owner | 0 | BR-6, and the direct answer to A6. |
| **M6** Time from bill arrival to a person being notified | Minutes | The current process has no defined answer to this. |

M1 and M2 are reported in `docs/testing-and-evaluation.md` from a labelled
evaluation set. M3 to M5 are structural: the schema does not permit a violation.

---

## 8. Explicitly not required

Recorded so that the absences read as decisions. Full reasoning in
`BUILD-SCOPE-v1.md`.

- **No approval, rejection, payment or status change.** Section 6.
- **No goods-receipt matching.** There is no goods-receipt record in scope, so
  this is two-way matching. The term three-way matching is not used.
- **No supplier communication.** The system routes internally.
- **No writes to the accounting system.** v1 reads only.
- **No replacement of the existing approval workflow.** This sits before it.
- **No production data.** Synthetic, builder-created fixtures only.

---

## 9. Traceability

| Requirement | Where it is met |
|---|---|
| BR-1 | Deterministic reconciliation (Volume 06), n8n polling and processing (Volume 07) |
| BR-2 | The exception-code set in Volume 06 sections 9.3 and 9.6 |
| BR-3 | `policy_service/domain/routing.py`, Volume 06 section 9.12 |
| BR-4 | The invocation gate and bounded contract in Volume 08 |
| BR-5 | The run and decision schema in Volume 03, Slack decision capture in Volume 09 |
| BR-6 | Error-handler workflow (Volume 07), failed-runs view and alert, `docs/runbook.md` |
| Section 6 boundary | Invariants I01, I02, I03, I05, I06, I26, I27. See `docs/invariant-register-v1.md`. |

---

## 10. Questions a real stakeholder would have to answer

These are unresolved because there is no stakeholder. They are listed because
their absence is a real limitation of this project, not a gap to paper over.

1. What tolerance is acceptable on quantity and unit price before something is an
   exception rather than rounding? The build uses zero tolerance on quantity and
   small absolute tolerances elsewhere; a real team would have a policy.
2. Which discrepancies does finance own and which does procurement own? The
   routing table encodes an assumption.
3. What is the actual exception rate, and is it rising? A2 is invented.
4. Does an exception have a deadline, and what happens when it passes? The build
   has no escalation timer.
5. Who is allowed to close a duplicate, and does that need a second pair of eyes?
6. What retention period applies to the decision record?

Questions 1, 2 and 4 would change the design. The rest would change
configuration.
