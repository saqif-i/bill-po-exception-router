# Where AI is used, and where it is not


This system automates a financial checking process. It uses a language model for
exactly one thing, and refuses to use it for everything else. This document
explains the line, why it is drawn there, and how the code enforces it rather
than merely intending it.

The short version: **the model is asked one question, about one pair of strings,
only after every number already agrees, and its answer changes no outcome.**

---

## 1. The test that decides

For each decision in the process, one question:

> Does a correct answer exist that can be computed from the data, or does it
> require reading meaning out of natural language written by someone outside the
> organisation?

If a correct answer can be computed, a model must not be used. Not because a
model would fail, but because a rule can be tested exhaustively, produces the
same answer every time, and can be explained to a finance team in a sentence. A
model brings variance and an explanation problem to a question that had neither.

If the answer requires reading meaning, a rule cannot do it, and the honest
options are to send it to a person or to ask a model for an opinion and then send
it to a person anyway.

Applying that test to this process leaves one decision on the model's side of the
line.

---

## 2. Decision inventory

| Decision | Mechanism | Why |
|---|---|---|
| Is this bill a supplier bill, and eligible? | Deterministic | Type and status are enumerated fields. |
| Does it reference a purchase order, and does that order exist and qualify? | Deterministic | Lookup and status check. |
| Is this a duplicate? | Deterministic | Invoice number and a business key. A rule cannot be talked into missing one. |
| Do supplier and currency match? | Deterministic | Identifier comparison. |
| Do quantity, unit price, line amount, tax amount and totals agree within tolerance? | Deterministic | Arithmetic. `ROUND_HALF_UP` on decimals, never floats. |
| Does the tax type match? | Deterministic | Enumerated value. |
| Is the account code present, correctly formatted, in the recorded chart, and equal on both sides? | Deterministic | Set membership and exact comparison after trim-only canonicalisation. |
| Which lines pair with which? | Deterministic | Three pairing tiers, none of which uses a model. |
| Which team owns this exception? | Deterministic | A routing table keyed on the exception reason. |
| May the model be consulted at all? | Deterministic | Twelve conditions (section 3). |
| **Do these two line descriptions refer to the same item?** | **Model, advisory only** | **The only decision requiring meaning from free text.** |
| What happens to the bill? | Human | A financial control. Not delegated to either layer. |

Ten deterministic, one advisory, one human. The proportion is the design.

---

## 3. The one question, and the gate in front of it

The model is invoked only when a bill and its purchase order have been reconciled
down to a single residual pair: exactly one unpaired bill line, exactly one
unpaired purchase-order line, and **every numeric and coding comparison on those
two residual lines already passing**.

Twelve deterministic conditions must all hold. They are evaluated by
`reconcile()` and `_finalise()` in `policy_service/domain/reconciliation.py`,
where any exception code in `SEMANTIC_BLOCKING_CODES`
(`policy_service/domain/enums.py`) closes the gate:

1. The bill passes eligibility. An `UNPROCESSABLE` bill never reaches the gate.
2. The bill names a purchase order that exists, is on the active fixture
   allow-list, and has an eligible status.
3. Supplier and currency match.
4. Neither duplicate check fires: no repeated invoice number, no repeated
   business key.
5. Every already-paired line agrees on quantity, unit price, line amount, tax
   type and line tax.
6. Every already-paired line has account codes that are present, format-valid,
   known in the recorded chart, and exactly equal.
7. Header tax and the bill total agree with the purchase order within tolerance.
8. The bill and the purchase order have the same number of lines.
9. Exactly one line remains unpaired on each side.
10. The residual pair agrees on quantity, unit price, line amount, tax type and
    tax amount.
11. The residual pair's account codes are present, format-valid, known in the
    recorded chart, and exactly equal.
12. Both residual descriptions are non-empty and no longer than 500 characters
    after normalisation (`MAX_RESIDUAL_DESCRIPTION_CHARS`). An over-long
    description **fails the gate rather than being truncated**, because
    truncating changes the thing being compared.

Two further checks sit outside the deterministic gate. `SEMANTIC_REVIEW_ENABLED`
must have been on when the run was reconciled; the captured flag is recorded, so
a later configuration change cannot reinterpret the run. And immediately before
the call, `load_eligible_run()` (`policy_service/domain/semantic.py`) re-checks
the persisted state: the run is still `REVIEW_READY` with outcome
`REVIEW_REQUIRED`, the stage is `PENDING`, and the recorded gate is open with a
clean residual pair.

**Numeric agreement on the residual pair is the one that matters.** Condition 9
makes a one-string-against-one-string prompt structurally sound. Conditions 10
and 11 make it meaningful. Without them, a residual pair differing on price as well as wording
would be sent as though wording were the only open question, and a
`LIKELY_EQUIVALENT` answer would actively mislead the person reading the card.
This is invariant I26.

When the gate refuses, the reason is recorded on the reconciliation result **for
every run, whether or not a call was made**. The gate's behaviour is auditable
rather than inferred, and a test asserts that the account-code gate set is
excluded before any model call is constructed.

**The measured effect of the gate is reported in the
[README results](../README.md#results).** The gate is not a claim; it is a number.

---

## 4. What the model may return

Four fields. Nothing else is accepted.

| Field | Values |
|---|---|
| `recommendation` | `LIKELY_EQUIVALENT`, `LIKELY_DIFFERENT`, `INSUFFICIENT_EVIDENCE` |
| `confidence` | 0 to 1 |
| `explanation` | One or two plain sentences, 400 characters or fewer, and may not tell anyone what to do: approve, reject, authorise, "should be paid" |
| `evidence` | One to four spans copied verbatim from the two supplied descriptions |

Note what is absent. **There is no field for an amount, a quantity, a status, an
action or an approval.** The contract has no channel through which the model
could affect an outcome even if it tried, and no channel through which text
inside a supplier description could instruct it to.

### The explanation validator was narrowed, deliberately

An earlier version of this check also blocked the nouns `price`, `amount`,
`total`, `quantity` and `tax`, matched as substrings.

That rejected **correct** answers. Comparing "Water, bottled, case of 24" with
"24x 500ml bottled water" is a question about a quantity, and any useful
explanation says so. Substring matching also caught `tax` inside `taxonomy`.

The list now targets **action and authority only**, on whole words. The
structural containment above is what actually prevents harm, because the
response has no field for an outcome; this check was always a backstop. An
over-broad backstop does more damage than none, because the first time it fires
on something correct you learn to distrust it.

`INSUFFICIENT_EVIDENCE` is documented to the model as a correct answer rather
than a failure. A model given only two acceptable answers will pick one; giving
it a way to decline is what makes the other two informative.

**Two schemas, not one.** The model-facing schema omits numeric and length bounds
because the structured-output subset strips them, and describes those bounds in
prose so the model still sees the intent. A strict schema is then applied
server-side after the response returns. Validation happens where it can be
enforced, not where it can be requested.

---

## 5. Failure is a designed path, not an exception

Eight things can go wrong with a model call. All eight land in the same place.
Each is recorded as its own review reason (`HumanReviewReason` in
`policy_service/domain/enums.py`).

| Failure | Review reason | Result |
|---|---|---|
| Provider does not answer in time | `SEMANTIC_TIMEOUT` | Run routes to a human. No recommendation shown. |
| Provider unreachable or returns an error status, including rate limits | `SEMANTIC_PROVIDER_UNAVAILABLE` | Same. |
| Output malformed, or fails the strict schema | `SEMANTIC_OUTPUT_INVALID` | Same. |
| Evidence is not an exact substring of the supplied descriptions | `SEMANTIC_EVIDENCE_UNSUPPORTED` | Same. |
| Model refuses | `SEMANTIC_REFUSED` | Same. |
| Output truncated | `SEMANTIC_TRUNCATED` | Same. |
| Any other unexpected stop reason | `SEMANTIC_UNEXPECTED_STOP_REASON` | Same. |
| Confidence below `SEMANTIC_MIN_CONFIDENCE` | `SEMANTIC_LOW_CONFIDENCE` | Same. |

A model that returns `INSUFFICIENT_EVIDENCE` has not failed. That is the model
working correctly: it is a valid recommendation, recorded and shown on the card
like the other two values.

The run reaches `COMPLETED_WITHOUT_RECOMMENDATION` and the human sees the case
with no model output at all, rather than a hedged or partial one. This is
invariant I06.

**The reason this is cheap to guarantee: the human step is not a fallback.** It is
on the path for every reviewed run regardless. A model failure removes a piece of
context from a card that was going to a person anyway. There is no degraded mode,
because there was no automated mode to degrade from.

---

## 6. Untrusted input

A supplier writes the line description. It is untrusted. Five controls, strongest
first:

1. **The response contract has no field through which an instruction could take
   effect.** No action, status, amount, quantity or approval field exists.
2. **Every outcome routes to a human**, so injection cannot cause an automatic
   result because there are no automatic results on this path.
3. **Data is delimited, not interpolated.** The two descriptions are serialised as
   JSON inside a `<candidate_pair>` block, and the system prompt states that
   everything inside the block is untrusted data to be compared, not obeyed.
4. **Normalisation and bounds.** Unicode NFKC-normalised, whitespace
   collapsed, length capped by the gate (condition 12).
5. **Evidence must be an exact substring** of the supplied description. Injected
   instruction text cannot be dressed up as evidence unless it literally appears
   in the description, in which case it is displayed to a person as the supplier's
   own words.

Control 1 is the one that does the work. The other four reduce the frequency of
attempts; control 1 removes the payoff. **Structural containment beats prompt
hardening**, because a prompt instruction is a request and a missing field is
not.

---

## 7. What the model cannot do, enforced

Not aspirations. Each is an invariant with an enforcement point, listed in full in
`docs/invariant-register-v1.md`.

| | Invariant |
|---|---|
| Never approves, rejects, pays or changes a bill | I01 |
| Never produces `MATCHED`, never alters `reconciliation_outcome`; every reviewed run stays `REVIEW_REQUIRED` and reaches a human | I02 |
| Never asked to choose between candidates; two or more on either side route straight to review | I05 |
| Missing, malformed, refused, truncated, unsupported or low-confidence output routes to review with nothing displayed | I06 |
| Invoked only on a residual pair that has passed every deterministic check | I26 |
| Never pairs the two residual lines; neither does the provisional comparison | I27 |
| No Xero data is used to train, fine-tune or adapt any model, and none is published in a training or evaluation dataset | I19 |

I02 is the load-bearing one. A model recommendation is displayed as context on a
Slack card. **It is never a routing input.** Routing is computed from the
deterministic exception reason. Two runs with identical exceptions and opposite
model recommendations route identically.

---

## 8. Measuring it

Claims about a model's behaviour are worth what the measurement behind them is
worth. This project keeps two labelled datasets:

- `evaluations/datasets/account_code_gate_v1.jsonl`, cases that must **never**
  reach the model. The evaluation asserts 100 percent deterministic exclusion
  before any model behaviour is scored. A failure here is a correctness bug, not
  a quality metric.
- `evaluations/datasets/line_semantics_v1.jsonl`, wording cases that legitimately
  reach the model, labelled with the answer a competent reviewer would give.

`evaluations/run_eval.py` writes a results file covering the invocation rate, the
distribution across the three recommendation values, agreement with the labels,
the refusal and invalid-output rates, and the gate-exclusion result. Prompt and
model versions are recorded with every run, so a change in numbers can be
attributed.

**`INSUFFICIENT_EVIDENCE` is scored as correct when the label says the text does
not support a view.** A system optimised only for agreement would train itself out
of the most useful answer it can give.

---

## 9. What I would revisit

Stated because a design with no known weaknesses has not been examined hard
enough.

**The gate may be too strict to be useful.** Requiring every other check to pass
before the model is invoked means it sees only the cleanest ambiguity. Real
exceptions often have two problems at once. The correct extension is more
residual-pair comparisons in the deterministic layer, not a looser gate.

**Confidence is a model self-report, not a calibrated probability.** It is used
only as a display threshold. Treating it as a probability would be a mistake, and
the current design avoids that by never letting it gate an outcome.

**Single-line residual only.** Two unpaired lines on each side route to a human
even when a person would see the answer instantly. Extending to small residual
sets would need a pairing contract that cannot be satisfied by a model asserting a
pairing, which is exactly what I27 forbids.

**The deterministic layer is where the real leverage is.** Every improvement there
reduces the model's job. That is the direction of travel: the invocation rate
going down over time is the system getting better, not worse.
