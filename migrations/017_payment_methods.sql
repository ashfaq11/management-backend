-- Every money movement records how it was paid: plan sales, top-ups and expenses as well as appointment payments.
ALTER TABLE customer_plans ADD COLUMN IF NOT EXISTS payment_method TEXT
  CHECK (payment_method IN ('cash','upi','card','bank_transfer','other'));
ALTER TABLE plan_topups ADD COLUMN IF NOT EXISTS payment_method TEXT
  CHECK (payment_method IN ('cash','upi','card','bank_transfer','other'));
ALTER TABLE expenses ADD COLUMN IF NOT EXISTS payment_method TEXT
  CHECK (payment_method IN ('cash','upi','card','bank_transfer','other'));
-- records made before this did not say; mark them 'other'
UPDATE customer_plans SET payment_method='other' WHERE payment_method IS NULL AND paid_at IS NOT NULL;
UPDATE plan_topups SET payment_method='other' WHERE payment_method IS NULL AND paid_at IS NOT NULL;
UPDATE expenses SET payment_method='other' WHERE payment_method IS NULL;
COMMENT ON COLUMN customer_plans.payment_method IS 'How the plan cost was paid (cash, upi, card, bank_transfer, other); NULL while pending.';
COMMENT ON COLUMN plan_topups.payment_method IS 'How the top-up was paid (cash, upi, card, bank_transfer, other); NULL while pending.';
COMMENT ON COLUMN expenses.payment_method IS 'How the expense was paid (cash, upi, card, bank_transfer, other).';
