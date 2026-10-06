-- 005c_escalation_and_information.sql
-- Escalation and information requests (ADR-010). Applied as bpr_owner. Forward-only.
--
-- "Escalate" and "Request more information" stop closing the run. Escalate
-- hands the case to #ap-escalations with a reason, and "Send back" returns it
-- to the team that escalated it. "Request more information" records a question
-- on the current card, and "Information received" records the answer. All four
-- are decisions that are not final and carry the note the person typed. Exactly
-- one final decision per run still holds, and a run is escalated at most once.
--
-- Rows recorded before this migration, when Escalate and Request more
-- information were final and carried no note, are history: the constraints
-- they would fail are added NOT VALID, so they are kept exactly as recorded.
--
-- Lettered 005c for the reason 005b is (BUILD-SCOPE-v1.md section 4): 006 to
-- 008 are reserved, and the runner refuses a migration that sorts before one
-- already applied.
--
-- No BEGIN or COMMIT, like 005b. The runner sends the file as one query, which
-- PostgreSQL runs as one transaction, and the migration test runs it inside a
-- transaction of its own that it then rolls back.
-- ---------------------------------------------------------------------------

-- The escalation reason, send-back note, question or answer. Stored trimmed;
-- the handler refuses an empty or over-long note before it gets here (I43).
ALTER TABLE triage_decisions ADD COLUMN note TEXT NULL;

ALTER TABLE triage_decisions
  ADD CONSTRAINT triage_note_bounded CHECK (
        note IS NULL OR char_length(note) BETWEEN 1 AND 500);

-- The four actions that ask for a note always carry one. NOT VALID: the final
-- Escalate and Request more information rows recorded before this had none.
ALTER TABLE triage_decisions
  ADD CONSTRAINT triage_note_required CHECK (
        action NOT IN ('ESCALATE', 'SEND_BACK', 'REQUEST_MORE_INFORMATION',
                       'INFORMATION_RECEIVED')
        OR note IS NOT NULL)
  NOT VALID;

-- I03 still: none of the new controls is named Approve or Reject.
ALTER TABLE triage_decisions DROP CONSTRAINT triage_action_valid;
ALTER TABLE triage_decisions
  ADD CONSTRAINT triage_action_valid CHECK (action IN (
        'MARK_REVIEWED', 'SEND_TO_FINANCE', 'SEND_TO_PROCUREMENT',
        'REQUEST_MORE_INFORMATION', 'ESCALATE', 'CLOSE_AS_DUPLICATE',
        'SEND_BACK', 'INFORMATION_RECEIVED'));

-- None of these closes a run. NOT VALID: Escalate and Request more information
-- were final before this migration, and those rows stay as they were recorded.
ALTER TABLE triage_decisions DROP CONSTRAINT triage_handoff_is_not_final;
ALTER TABLE triage_decisions
  ADD CONSTRAINT triage_handoff_is_not_final CHECK (
        action NOT IN ('SEND_TO_FINANCE', 'SEND_TO_PROCUREMENT', 'ESCALATE',
                       'SEND_BACK', 'REQUEST_MORE_INFORMATION',
                       'INFORMATION_RECEIVED')
        OR NOT is_final)
  NOT VALID;

-- I42: a run is escalated at most once. Only one final decision per run was
-- ever allowed, so no existing run has two Escalate rows.
CREATE UNIQUE INDEX uq_triage_one_escalation_per_run
    ON triage_decisions (run_id) WHERE action = 'ESCALATE';

ALTER TABLE slack_notifications DROP CONSTRAINT slack_destination_valid;
ALTER TABLE slack_notifications
  ADD CONSTRAINT slack_destination_valid CHECK (destination IN (
        'FINANCE', 'PROCUREMENT', 'AP_REVIEW', 'DUPLICATE_REVIEW', 'ESCALATED'));

-- New: runs.triage_destination had no CHECK. Every value written so far came
-- from the routing enum, so this one is validated.
ALTER TABLE runs
  ADD CONSTRAINT runs_triage_destination_valid CHECK (
        triage_destination IS NULL
        OR triage_destination IN ('FINANCE', 'PROCUREMENT', 'AP_REVIEW',
                                  'DUPLICATE_REVIEW', 'ESCALATED'));
