-- A plan or top-up can be sold on credit: paid_at NULL = payment pending; revenue counts it when it is paid.
ALTER TABLE customer_plans ADD COLUMN IF NOT EXISTS paid_at TIMESTAMPTZ;
ALTER TABLE plan_topups ADD COLUMN IF NOT EXISTS paid_at TIMESTAMPTZ;
UPDATE customer_plans SET paid_at = created_at WHERE paid_at IS NULL;
UPDATE plan_topups SET paid_at = created_at WHERE paid_at IS NULL;
COMMENT ON COLUMN customer_plans.paid_at IS 'When the plan cost was received; NULL = payment pending. Plan sales count on this date.';
COMMENT ON COLUMN plan_topups.paid_at IS 'When the top-up cost was received; NULL = payment pending. Plan sales count on this date.';
