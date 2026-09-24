# Responsible AI

Short, because the substance is in
[`ai-vs-deterministic-decisions.md`](ai-vs-deterministic-decisions.md) and in the
code. This records the commitments.

## The model is asked one question

Whether two short line descriptions refer to the same item. Not whether the bill
is correct, whether it should be paid, or what anyone should do next. No field
exists in the response for any of those.

## It cannot reach an outcome

Enforced structurally rather than by instruction:

- The response contract has four fields, and none is an amount, a quantity, a
  status, an action or an approval.
- `semantic_attempts` has no column for an outcome or a decision.
- Routing is computed from the deterministic exception codes. Two runs with
  identical exceptions and opposite recommendations route identically.
- Every reviewed run reaches a human whether the model succeeded, failed,
  refused or was never consulted.

## It is consulted rarely, and only when nothing else is in doubt

Twelve gate conditions must hold, the significant one being that **every
deterministic check on the residual pair has already passed**. Wording must be
the sole unresolved difference. The gate reason is recorded on every run,
including matched ones, so the gate's behaviour is auditable rather than
inferred.

## Absence is a valid answer

`INSUFFICIENT_EVIDENCE` is documented to the model as correct rather than as a
failure, and the evaluation harness scores it as correct when the label says the
text does not support a view. A harness optimised only for agreement would train
the system out of the most useful answer it can give.

## Data

No data obtained from the Xero API is used to train, fine-tune, adapt or enhance
any model, and none is published in a training or evaluation dataset (I19). The
evaluation datasets in `evaluations/datasets/` are written by hand and contain
no provider data.

All data in this project is synthetic, created by the builder in a Xero demo
company (I16). Public portfolio material contains nothing else.

## Untrusted input

Line descriptions are written by suppliers and are treated as untrusted. Five
controls, strongest first: the contract has no actionable field; every outcome
reaches a human; data is delimited as JSON inside a named block rather than
interpolated; text is normalised and length-bounded; and evidence must be a
verbatim substring of the supplied description.

The first removes the payoff. The rest reduce the frequency of attempts.
Structural containment beats prompt hardening, because a prompt instruction is a
request and a missing field is not.

## What a person still owns

The decision. Slack controls express operational triage, and no control is named
Approve or Reject, because none of them is one (I03). Nothing in this system
approves, rejects, pays or changes the status of a bill.
