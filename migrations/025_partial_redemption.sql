-- Part-paid visits: a membership short of the visit covers part, the rest is collected.
ALTER TABLE plan_redemptions ADD COLUMN IF NOT EXISTS covered NUMERIC(12,2) CHECK (covered >= 0);
ALTER TABLE plan_redemptions ADD COLUMN IF NOT EXISTS created_by BIGINT;
COMMENT ON COLUMN plan_redemptions.covered IS 'Rupee value of the visit this deduction paid for; NULL = the whole visit.';
COMMENT ON COLUMN plan_redemptions.created_by IS 'Login that made the deduction.';
