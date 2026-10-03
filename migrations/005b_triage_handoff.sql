-- 005b_triage_handoff.sql
-- Hand-offs. Applied as bpr_owner. Forward-only.
--
-- "Send to finance" and "Send to procurement" hand a case to that team rather
-- than closing it. The hand-off is recorded as a decision that is not final:
-- the run moves to the other team's channel with a new card, and that team's
-- decision is the one that closes it. Any number of hand-offs, then exactly one
-- final decision per run. Rows stay immutable (the trigger from 005 still
-- applies), so the record shows who handed the case over and who decided it.
--
-- Numbered 005b, not 006: 006 to 008 are reserved for deferred components
-- (BUILD-SCOPE-v1.md section 4), and the runner refuses a migration that sorts
-- before one already applied. "005b_" sorts after "005_" and before "006".
-- ---------------------------------------------------------------------------

ALTER TABLE triage_decisions ADD COLUMN is_final BOOLEAN NOT NULL DEFAULT TRUE;

ALTER TABLE triage_decisions DROP CONSTRAINT uq_triage_one_per_run;

CREATE UNIQUE INDEX uq_triage_one_final_per_run
    ON triage_decisions (run_id) WHERE is_final;

-- A hand-off never closes a run. NOT VALID: rows recorded before this
-- migration, when a hand-off was final, are left as they were recorded.
ALTER TABLE triage_decisions
  ADD CONSTRAINT triage_handoff_is_not_final CHECK (
        action NOT IN ('SEND_TO_FINANCE', 'SEND_TO_PROCUREMENT') OR NOT is_final)
  NOT VALID;
