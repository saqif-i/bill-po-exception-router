# Invariant register, v1

The forty operational invariants were fixed before the build and cover the full
design, including components v1 defers (listed in `BUILD-SCOPE-v1.md` section 2).
No implementation detail may weaken an invariant, and where one appears to
conflict with an invariant, the invariant wins.

v1 does not weaken any invariant. It builds a smaller system, and some
invariants govern machinery that smaller system does not contain. This register
records the status of every one of the forty, with a reason. Nothing is dropped
silently.

## Summary

| Status | Count | Meaning |
|---|---|---|
| Enforced | 23 | Governs something v1 builds, and is enforced there. |
| Enforced, reduced surface | 3 | Still enforced. The thing it governs is smaller or absent in v1, which makes the guarantee easier to hold, not harder. |
| Deferred | 3 | Governs machinery v1 does not build. Would be re-activated with the deferred component that introduces it. |
| Not applicable | 11 | Has no subject in v1. Every one of these governs the transactional outbox, the Xero write attempt, or replay. |

**All 11 not-applicable invariants trace to one decision:** v1 performs no
external mutation. There is no Xero write, therefore no durable action intent,
no outbox, no lease fence on a command record, no replay, and no unknown
outcome to reconcile. That decision is recorded in `BUILD-SCOPE-v1.md` with the
reasoning behind it.

The three deferred invariants (I24, I33, I34) are the generic lease and fencing
rules. They are deferred rather than not applicable because the deferred recovery and
replay or fixture reset components would introduce leased operations that need them, whereas the eleven above would
require reintroducing the Xero write path itself.

## The register

| ID | Status in v1 | Invariant | Reason |
|---|---|---|---|
| **I01** | Enforced | Claude never approves, rejects, pays or changes a bill. | The model contract (`policy_service/integrations/claude_contract.py`) has no field for an amount, status, action or approval, and the Xero transport allow-list has no write method. |
| **I02** | Enforced | Claude never produces `MATCHED` and never alters `reconciliation_outcome`. Every Claude-reviewed run keeps `reconciliation_outcome = REVIEW_REQUIRED` and must reach a human. | `policy_service/domain/semantic.py` never writes `reconciliation_outcome`; the `runs` CHECK constraints in `migrations/001_core_schema.sql` and `003_reconciliation.sql` require `REVIEW_REQUIRED` on every review-path status. |
| **I03** | Enforced | Slack controls express operational triage decisions, not financial approval. No control is named Approve or Reject. | Card controls in `policy_service/integrations/slack_blocks.py`, asserted by `tests/unit/test_slack_card.py`. |
| **I04** | Enforced | Quantity, unit price, line amount, tax amount, tax type, account-code presence, format, chart membership and exact bill-to-purchase-order equality, totals, currency, supplier identity, purchase-order validity and duplicate detection are deterministic and final, and cannot be influenced by AI. | `policy_service/domain/reconciliation.py` and `duplicates.py`, pure functions with no model input. |
| **I05** | Enforced | Two or more semantic candidates on either side route straight to human review. Claude is never asked to choose among candidates. | The gate in `reconcile()` records `MULTIPLE_RESIDUAL_CANDIDATES` and closes. |
| **I06** | Enforced | Missing, malformed, refused, truncated, unsupported-evidence or low-confidence model output routes to human review with no recommendation displayed. | Validation in `policy_service/integrations/claude_contract.py`; `finalise()` in `policy_service/domain/semantic.py` stores no recommendation on any failure. |
| **I07** | Enforced, reduced surface | Normal runtime permits exactly one Xero mutation: adding a history note to an allow-listed bill. Demo seeding is a separate program with separate credential handling. | v1 performs no Xero mutation of any kind. The one permitted history note belongs to the deferred durable outbox. Enforced by absence: no write method exists on the transport allow-list. |
| **I08** | Enforced | Missing or invalid write-mode configuration fails closed. The service refuses to start on an unknown value and resolves an unset value to `disabled`. | The write-mode validator in `policy_service/config.py`. |
| **I09** | Enforced | Every workflow execution has a correlation ID, propagated to every event, log line, model call and downstream request. Correlation IDs supplied by unauthenticated external ingress are never trusted; the service generates its own. | The correlation-id middleware in `policy_service/main.py`, which generates its own id on every request, including Slack ingress. |
| **I10** | Enforced | Every externally triggered operation has an application-level idempotency key. A provider-native mechanism is used **in addition**, only where the provider documents one for the exact operation. | The ingestion version key in `policy_service/domain/ingestion.py` and the `Idempotency-Key` contract in `policy_service/api/idempotency.py`. |
| **I11** | Enforced, reduced surface | A human triage decision and its resulting `XERO_HISTORY_NOTE` action are committed in one transaction **before** Slack is acknowledged. No `SLACK_RESULT_UPDATE` is created in that transaction. | The triage decision is still committed before the Slack acknowledgement. The `XERO_HISTORY_NOTE` half of the transaction has no subject in v1. |
| **I12** | Enforced | No database transaction, row lock or PostgreSQL advisory lock is held open across a call to Xero, Slack or Anthropic. | `review()` in `policy_service/domain/semantic.py` commits the attempt before the provider call and finalises in a new transaction; `policy_service/domain/triage.py` commits the decision before calling Slack. |
| **I13** | Enforced | A syntactically valid, positive `Retry-After` is never shortened, and is never reclassified as malformed merely for being large. A worker sleep ceiling governs in-process blocking only. | `policy_service/integrations/xero_transport.py`, asserted by `tests/unit/test_xero_transport.py`. |
| **I14** | Enforced | Only records on the active fixture allow-list may become runs, be sent to a model provider, become write-eligible, or appear in public material. A non-allow-listed record never becomes a run. | Enforced twice: `check_allow_list()` in `policy_service/domain/ingestion.py` admits only records mapped to an `ACTIVE` row in `seed_fixtures`, and the `trg_runs_require_active_fixture` trigger in `migrations/001_core_schema.sql` refuses any run whose fixture is not `ACTIVE`. |
| **I15** | Enforced | Secrets are never written to logs, fixtures, workflow exports, screenshots, evaluation data or Git. | `policy_service/security/redaction.py` in the log formatter, `scripts/check_workflow_exports.py`, and gitleaks in CI and the pre-commit script. |
| **I16** | Enforced | Public portfolio material contains synthetic, builder-created data only. | Re-homed. `docs/responsible-ai.md` and the demo recording. Synthetic, builder-created data only. |
| **I17** | Enforced | Live tests run only against the Xero Demo Company, are explicitly marked, and never run on public pull-request CI. | The `live` marker in `pytest.ini`, excluded by default and by CI. |
| **I18** | Not applicable | A failed downstream action remains recoverable from PostgreSQL. No work is lost because a process died, a lease expired or a response was lost. | Recoverability from PostgreSQL after a lost lease or dead process is the purpose of the outbox. v1 has no outbox and no durable action intent. |
| **I19** | Enforced | No data obtained from the Xero API is used to train, fine-tune, adapt or enhance any AI model, and none is published in a training or evaluation dataset. | `docs/responsible-ai.md`. The evaluation datasets are hand-written, not Xero data. |
| **I20** | Enforced, reduced surface | Immutable records are never mutated by a retention process. Purgeable content lives in separate tables from immutable metadata and may be removed only by the bounded `bpr_owner`-owned purge function; the runtime retention role has no direct table access. | The table separation is enforced in `001_core_schema.sql`. No retention process runs in v1, so the non-mutation clause has nothing to violate. |
| **I21** | Not applicable | A `SLACK_RESULT_UPDATE` is created only when the exact Xero action generation it reports becomes terminal, in the same transaction as that terminal transition. While Xero is `PENDING`, `PROCESSING` or `RETRY_WAIT`, no result action exists. | Governs `SLACK_RESULT_UPDATE` creation against a terminal Xero action generation. No Xero action exists in v1. |
| **I22** | Not applicable | The mandatory non-approval disclaimer in a Xero history note is never removed, shortened or truncated. | Governs the disclaimer inside a Xero history note. No history note is written in v1. |
| **I23** | Enforced | The project makes no claim about any organisation's internal architecture, systems, processes or roadmap, and claims no formal compliance conclusion. | Re-homed. `docs/limitations-and-roadmap.md` and `docs/business-requirements.md`, which states its scenario is synthetic. |
| **I24** | Deferred | Every leased operation carries a claim generation. A result is accepted only from the worker and generation that currently holds the claim; a stale result is discarded and recorded, never applied. | v1 runs no leased operations. The generic lease and fencing machinery is deferred with the recovery workers that need it. |
| **I25** | Not applicable | A run reaches `COMPLETED` only when every outbox action for it has reached a terminal state and the latest Slack result update has succeeded. A run never remains `COMPLETED` once one of its actions returns to `PENDING`, `PROCESSING` or `RETRY_WAIT`. | Defines `COMPLETED` in terms of terminal outbox actions. v1 has no outbox actions. |
| **I26** | Enforced | Claude is invoked only when the single residual bill line and the single residual purchase-order line have been compared deterministically and agree on quantity, unit price, line amount, tax type and tax amount within tolerance, and both account codes are present, format-valid, known in the recorded chart-of-accounts reference and exactly equal after trim-only canonicalisation. | The gate in `reconcile()`, re-checked before the call by `load_eligible_run()` in `policy_service/domain/semantic.py`. |
| **I27** | Enforced | The provisional residual comparison never pairs the two lines, and no model recommendation ever pairs them. `MATCHED` is unreachable from any path involving a residual pair. | The residual comparison in `policy_service/domain/reconciliation.py` emits separate `RESIDUAL_*` codes and never pairs; routing ignores any recommendation. |
| **I28** | Enforced | A review case's `reconciliation_outcome` and its next `workflow_status` are persisted in the same transaction as its results and exceptions. A run is never observable with an outcome set while still in `RECONCILING`. | One transaction in `policy_service/db/repository.py`, backed by the status and outcome CHECK constraints in `migrations/003_reconciliation.sql`. |
| **I29** | Not applicable | A Slack result update is immutably bound to one exact Xero action and one exact `replay_generation`, and is claimable only when that generation is terminal. | Binds a Slack result update to a Xero action and replay generation. Neither exists in v1. |
| **I30** | Not applicable | An outcome that is not known is reported as unknown. Any Xero attempt whose lease expires after dispatch may have begun records terminal `OUTCOME_UNKNOWN`, never a confirmed failure, and is not retried automatically. | `OUTCOME_UNKNOWN` reports an external mutation whose fate cannot be established. v1 attempts no external mutation. |
| **I31** | Enforced | Notification locks a run observed in `REVIEW_READY` and requires `REVIEW_REQUIRED`, a terminal `semantic_stage_status` and no `STARTED` attempt. Correctness is enforced by the policy service and PostgreSQL, never by n8n ordering or concurrency settings. | `notify()` in `policy_service/domain/triage.py`. |
| **I32** | Enforced | A semantic provider result may finalise only its still-`STARTED` attempt while the run remains `REVIEW_READY` and `semantic_stage_status = IN_PROGRESS`. A result arriving after abandonment affects zero rows. | The conditional update in `finalise()` in `policy_service/domain/semantic.py`. |
| **I33** | Deferred | At most one `seed_reset_operations` row is open for `DEMO_FIXTURE_SET`. That durable row, its renewable lease and its monotonically increasing `claim_generation` are the authority throughout reset phases A to D. | Requires `seed_reset_operations` and the durable reset procedure from the deferred fixture reset machinery. v1 seeds fixtures directly and reseeds after a Demo Company reset. |
| **I34** | Deferred | Every reset progress write and activation is fenced by `operation_id`, `owner_id` and `claim_generation`. Fencing protects local authority but cannot undo a Xero write that already occurred. | Same dependency as I33. |
| **I35** | Not applicable | Every claimed processing attempt for a `XERO_HISTORY_NOTE` produces exactly one immutable `xero_write_attempts` row, including disabled writes, local validation failures, retryable responses, definitely-undispatched transport failures and sweeper recovery. | Governs immutable `xero_write_attempts` rows. No write attempts occur. |
| **I36** | Enforced | Every authenticated internal mutating endpoint requires a validated `Idempotency-Key`, scoped by authenticated caller and operation, committed before or with the first mutation, and unable to bypass eligibility. | `policy_service/api/idempotency.py` and the `idempotency_registry` table, committed with the first mutation by `policy_service/db/idempotency_store.py`. |
| **I37** | Not applicable | Manual replay is action-specific and always restores the run. Every replay transaction locks the target action **and** its run, and commits the action reset together with exactly one of three recovery-only run transitions. No Xero or Slack call occurs inside the replay transaction. | Manual replay operates on outbox actions. None exist. |
| **I38** | Not applicable | A manual Xero reconciliation result is one of `CONFIRMED_WRITTEN`, `CONFIRMED_NOT_WRITTEN` or `INCONCLUSIVE`, recorded immutably against one action and one Xero replay generation. **Only `CONFIRMED_NOT_WRITTEN` authorises another Xero write attempt.** `CONFIRMED_WRITTEN` closes the action without a further write and creates the appropriate Slack result intent. `INCONCLUSIVE` remains blocked from replay. | Manual Xero reconciliation resolves an unknown write outcome. No writes, no unknown outcomes. |
| **I39** | Not applicable | Every mutation of an `/outbox/process` command record requires a live lease under the full command fence: exact `ledger_id`, fixed endpoint scope, `status = PROCESSING`, exact `owner_id`, exact `claim_generation` and `lease_expires_at > now()`. A zero-row update means ownership was lost and the worker stops without changing command state. | Governs the `/outbox/process` command fence. The endpoint is not built. |
| **I40** | Not applicable | Provider time and database finalisation time have separate bounded budgets. The provider transport receives only the provider budget, and the action deadline is at least the provider budget plus the finalisation budget. | Splits provider and finalisation time budgets for the Xero write path. Not built. |

## Re-homed invariants

Five invariants were to be enforced by components v1 does not build, but they
are policy rather than machinery and cost nothing to keep. Their enforcement
point moves:

| ID | Now enforced in v1 by |
|---|---|
| **I14** | The `seed_fixtures` table and its `trg_runs_require_active_fixture` trigger (`migrations/001_core_schema.sql`), and `check_allow_list()` in `policy_service/domain/ingestion.py`. |
| **I16** | `docs/responsible-ai.md` and the demo recording. Synthetic data only. |
| **I17** | `tests/live/` with its pytest marker and CI exclusion. **This directory is retained in v1 even though the live test count is small**, because I17 requires the mechanism to exist, not merely that no live test happens to run. |
| **I19** | `docs/responsible-ai.md` and the provider configuration recorded in `docs/security.md`. |
| **I23** | `docs/limitations-and-roadmap.md`. No claim about any organisation's internal systems, no compliance conclusion. |

## What would change these statuses

Building the durable outbox moves I07 back to its full surface and re-activates
I21, I22, I29, I35 and I40. Building recovery and replay re-activates I18, I25,
I30, I37 and I38, and requires I24 and I39. Building the fixture reset machinery
re-activates I33 and I34. Nothing in v1 blocks any of that: the repository tree
is a strict subset of the full design's tree (`BUILD-SCOPE-v1.md` section 4), so
every deferred path is still available under its reserved name.
