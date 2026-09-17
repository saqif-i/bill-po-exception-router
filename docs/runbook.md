# Runbook

Six failures, each one **deliberately triggered** before this was written. If a
symptom here does not match what you see, the runbook is wrong and should be
corrected rather than worked around.

How to use this: find the symptom, confirm it with the check, apply the action.
Every check is read-only.

---

## 1. Runs stuck in `INGESTED` or `RECONCILING`

**Symptom.** Bills are ingested and never reach a card.

**Trigger it.** Stop the service between poll and reconcile:
`docker compose stop policy_service`, run the poll workflow, start it again.

**Check.**
```bash
python scripts/capture_metrics.py | python3 -c "import json,sys; print(json.load(sys.stdin)['stuck_runs'])"
```

**Cause.** Workflow 02 did not run, or failed partway. The run exists; nothing
picked it up.

**Action.** Re-run workflow 02 for that run id from n8n. Reconciliation is
idempotent per run and per operation, so a repeat with the same key replays
rather than reconciling twice.

**Do not** delete the run. A new poll will not recreate it: the ingestion
version key is unchanged, so no second run is created, and the poll reports it
as `skipped_unchanged`.

That is the trap worth understanding. **Workflow 01 only calls workflow 02 for
runs it ingested in that execution.** A run stranded at `INGESTED` by an earlier
poll is never picked up again, because the poll is the only thing that triggers
reconciliation. Automatic recovery is Volume 11 and is deferred; until then the
loop below is the recovery:

```bash
export $(grep -E '^INTERNAL_BEARER_TOKEN=' .env | xargs)

docker compose exec -T postgres psql -U bpr_owner -d bpr -Atc \
  "SELECT run_id FROM runs WHERE workflow_status = 'INGESTED';" |
while read -r id; do
  printf '%s -> ' "$id"
  curl -sS -o /dev/null -w '%{http_code}\n' -X POST "localhost:8000/runs/$id/reconcile" \
    -H "Authorization: Bearer $INTERNAL_BEARER_TOKEN" \
    -H "Idempotency-Key: $id-reconcile"
done
```

---

## 1b. A poll returns `polled: 0` against bills you can see in Xero

**Symptom.** `/runs/poll` reports zero polled, but a direct Xero query returns
your bills.

**Check.**
```bash
docker compose exec postgres psql -U bpr_owner -d bpr -c "SELECT * FROM poll_cursors;"
```

**Cause.** The cursor. The poller asks Xero for bills modified **since the
watermark**, and the watermark advances after every successful poll **including
one that found nothing**. So a single empty poll narrows every poll after it.

That is correct in production, where bills keep arriving, and awkward while
rebuilding a fixed set of demo data.

**Action.** Truncate it **immediately before** re-running, not earlier in a
sequence of other steps:

```bash
docker compose exec postgres psql -U bpr_owner -d bpr -c "TRUNCATE poll_cursors;"
```

---

## 2. Everything is skipped as not allow-listed

**Symptom.** `POST /runs/poll` returns `ingested: 0` with a large
`skipped_not_allow_listed`.

**Trigger it.** `UPDATE seed_fixtures SET fixture_status='RETIRED';` then poll.

**Check.**
```sql
SELECT fixture_status, count(*) FROM seed_fixtures GROUP BY 1;
```

**Cause.** Invariant I14 working. Only records on the **active** allow-list
become runs, and the demo company's own sample invoices are not on it.

**Action.** If the count of `ACTIVE` is zero or wrong, repopulate from the
captured fixtures as in Part 6 Stage 6.11. If it looks right, confirm the
invoice ids in `seed_fixtures` match the ones Xero is returning; a demo company
reset changes them.

---

## 3. Readiness reports `MIGRATIONS_NOT_READY`

**Symptom.** `/readyz` returns 503 and lists missing migrations.

**Trigger it.** Add a filename to `REQUIRED_MIGRATIONS` that is not applied.

**Check.**
```bash
curl -s localhost:8000/readyz
docker compose exec postgres psql -U bpr_owner -d bpr -c "SELECT filename FROM schema_migrations ORDER BY 1;"
```

**Cause.** Either a migration genuinely has not run, or `bpr_app` cannot read
`schema_migrations`. The second looks identical from outside and is the one that
wastes an afternoon.

**Action.** Run `make migrate`. If the ledger already lists everything, check the
grant at the end of `001_core_schema.sql` survived.

---

## 4. The model stage never completes

**Symptom.** Runs sit in `REVIEW_READY` with `semantic_stage_status` of
`IN_PROGRESS`, and no card appears.

**Trigger it.** Kill the service during a semantic call.

**Check.**
```sql
SELECT run_id, attempt_number, started_at FROM semantic_attempts
 WHERE status = 'STARTED' ORDER BY started_at;
```

**Cause.** The attempt was committed before the provider call, by design, so a
crash leaves a visible `STARTED` row rather than a call nobody recorded.
Notification is then correctly refused, because I31 requires a terminal stage: a
card posted now would change under the reviewer.

**Action.** There is no automatic recovery in this build; that is Volume 11 and
it is deferred. Resolve by hand:

```sql
UPDATE semantic_attempts SET status='REJECTED',
       rejection_reason='SEMANTIC_PROVIDER_UNAVAILABLE', finalised_at=now()
 WHERE run_id = '<run>' AND status='STARTED';
UPDATE runs SET semantic_stage_status='COMPLETED_WITHOUT_RECOMMENDATION'
 WHERE run_id = '<run>';
```

Then re-run notify. The reviewer sees the case with no recommendation, which is
invariant I06 and the correct outcome.

---

## 5. Slack interactions return 401

**Symptom.** Buttons produce an error in Slack; the service logs
`slack signature rejected`.

**Trigger it.** Change `SLACK_SIGNING_SECRET` to anything else and click a
button.

**Check.** The log line carries the reason. It is never in the response, because
telling a caller which check failed helps them pass it next time.

| Reason | Meaning |
|---|---|
| `MISSING_SIGNATURE_HEADERS` | Not from Slack, or a proxy stripped headers |
| `TIMESTAMP_OUTSIDE_WINDOW` | Replay, or clock skew over five minutes |
| `SIGNATURE_MISMATCH` | Wrong secret, or the raw body was re-encoded |

**Action.** For `SIGNATURE_MISMATCH`, confirm the secret is from **Basic
Information**, not the bot token, and that nothing between Slack and the service
re-serialises the body. For `TIMESTAMP_OUTSIDE_WINDOW`, check the clock.

---

## 6. Two identical cards for one bill

**Symptom.** A duplicate card in a channel.

**Trigger it.** Make the Slack client time out during notify.

**Check.**
```sql
SELECT run_id, post_status, post_error FROM slack_notifications
 WHERE post_status <> 'POSTED';
```

**Cause.** A post that timed out was recorded as `POSSIBLE_DUPLICATE`, because
the outcome was genuinely unknown. Calling it a failure would be a claim the code
cannot support.

**Action.** None required. Delivery is at-least-once and this is documented, not
a defect. Decide on one card; the other is inert, and the unique constraint on
`run_id` means only one decision can be recorded either way.

Making this impossible needs a transactional outbox, which is deferred.

---

## Escalation

There is no on-call rotation. This is a portfolio project in a demo company with
synthetic data, and the honest answer to "who do I page" is nobody.

If the demo company has reset, the fixtures are gone and the recorded
demonstration is the durable artefact. Rebuild the fixtures from Part 1 Stage 1.7
and re-run `scripts/capture_fixtures.py`.
