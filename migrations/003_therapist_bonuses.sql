-- Extra incentives paid to a therapist on a given day (festival bonus, extra hours, a tip passed on, ...),
-- on top of the percentage incentive. They land on the payslip of the month of on_date.
CREATE TABLE therapist_bonuses (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  therapist_id BIGINT NOT NULL REFERENCES therapists (id),
  on_date DATE NOT NULL,
  amount NUMERIC(12,2) NOT NULL CHECK (amount > 0),
  note TEXT,
  appointment_id BIGINT REFERENCES appointments (id),
  created_by BIGINT REFERENCES users (id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX therapist_bonuses_therapist_idx ON therapist_bonuses (therapist_id, on_date);

COMMENT ON TABLE therapist_bonuses IS 'Extra incentives for a therapist on a date, added to that month''s payslip on top of the percentage incentive.';
COMMENT ON COLUMN therapist_bonuses.id IS 'Identity.';
COMMENT ON COLUMN therapist_bonuses.therapist_id IS 'Who gets it.';
COMMENT ON COLUMN therapist_bonuses.created_at IS 'When it was added.';
COMMENT ON COLUMN therapist_bonuses.on_date IS 'The day the incentive is for; decides which month''s payslip it is on.';
COMMENT ON COLUMN therapist_bonuses.amount IS 'INR, more than 0.';
COMMENT ON COLUMN therapist_bonuses.note IS 'Why, e.g. Festival bonus, Extra hours.';
COMMENT ON COLUMN therapist_bonuses.appointment_id IS 'The appointment it was given for, when it was given from the appointments list (optional).';
COMMENT ON COLUMN therapist_bonuses.created_by IS 'The user who added it.';
