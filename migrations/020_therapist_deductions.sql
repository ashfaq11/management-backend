-- Salary deductions (advance, leave, damage, fine, loan, other) with a reason; they reduce the month's net pay.
CREATE TABLE therapist_deductions (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  spa_id BIGINT NOT NULL REFERENCES spas(id),
  therapist_id BIGINT NOT NULL REFERENCES therapists(id),
  kind TEXT NOT NULL CHECK (kind IN ('advance','leave','damage','fine','loan','other')),
  amount NUMERIC(10,2) NOT NULL CHECK (amount > 0),
  reason TEXT NOT NULL CHECK (length(btrim(reason)) >= 3),
  on_date DATE NOT NULL,
  created_by BIGINT REFERENCES users(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX therapist_deductions_spa_date_idx ON therapist_deductions (spa_id, on_date);
CREATE INDEX therapist_deductions_therapist_idx ON therapist_deductions (therapist_id, on_date);
COMMENT ON TABLE therapist_deductions IS 'Amounts taken off a therapist''s salary in the month of on_date.';
COMMENT ON COLUMN therapist_deductions.id IS 'Identity.';
COMMENT ON COLUMN therapist_deductions.spa_id IS 'Owning spa.';
COMMENT ON COLUMN therapist_deductions.therapist_id IS 'Therapist whose pay is reduced.';
COMMENT ON COLUMN therapist_deductions.kind IS 'advance, leave, damage, fine, loan or other.';
COMMENT ON COLUMN therapist_deductions.amount IS 'Amount deducted (INR).';
COMMENT ON COLUMN therapist_deductions.reason IS 'Why it was deducted; printed on the payslip.';
COMMENT ON COLUMN therapist_deductions.on_date IS 'Date of the deduction; decides the payslip month.';
COMMENT ON COLUMN therapist_deductions.created_by IS 'Login that recorded it.';
COMMENT ON COLUMN therapist_deductions.created_at IS 'When it was recorded.';
