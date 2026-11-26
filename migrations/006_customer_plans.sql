-- Prepaid customer plans: one balance is either hours or INR, never both.
CREATE TABLE customer_plans (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  customer_id BIGINT NOT NULL REFERENCES customers(id),
  spa_id BIGINT NOT NULL REFERENCES spas(id),
  name TEXT NOT NULL CHECK (length(btrim(name)) > 0),
  plan_type TEXT NOT NULL CHECK (plan_type IN ('hours', 'rupees')),
  purchased_amount NUMERIC(12,2) NOT NULL CHECK (purchased_amount > 0),
  balance NUMERIC(12,2) NOT NULL CHECK (balance >= 0 AND balance <= purchased_amount),
  expires_on DATE,
  notes TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX customer_plans_customer_idx ON customer_plans (spa_id, customer_id, created_at);

CREATE TABLE plan_redemptions (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  plan_id BIGINT NOT NULL REFERENCES customer_plans(id),
  appointment_id BIGINT NOT NULL REFERENCES appointments(id),
  spa_id BIGINT NOT NULL REFERENCES spas(id),
  amount NUMERIC(12,2) NOT NULL CHECK (amount > 0),
  unit TEXT NOT NULL CHECK (unit IN ('hours', 'rupees')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  reversed_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX plan_redemptions_one_active_appointment
  ON plan_redemptions (appointment_id) WHERE reversed_at IS NULL;
CREATE INDEX plan_redemptions_plan_idx ON plan_redemptions (plan_id, created_at);

COMMENT ON TABLE customer_plans IS 'Prepaid package owned by one customer in one spa. A balance is measured in hours or INR.';
COMMENT ON COLUMN customer_plans.id IS 'Identity.';
COMMENT ON COLUMN customer_plans.customer_id IS 'Customer who purchased this plan.';
COMMENT ON COLUMN customer_plans.spa_id IS 'Owning spa; must match the customer and appointment.';
COMMENT ON COLUMN customer_plans.name IS 'Display name of the prepaid package.';
COMMENT ON COLUMN customer_plans.plan_type IS 'Balance unit: hours for service time, or rupees for service value.';
COMMENT ON COLUMN customer_plans.purchased_amount IS 'Original amount purchased, in the plan unit.';
COMMENT ON COLUMN customer_plans.balance IS 'Remaining amount, in the plan unit; reduced only when an appointment is completed.';
COMMENT ON COLUMN customer_plans.expires_on IS 'Optional last date on which this plan can be redeemed.';
COMMENT ON COLUMN customer_plans.notes IS 'Optional purchase or package notes.';
COMMENT ON COLUMN customer_plans.created_at IS 'When the plan was added to the customer.';

COMMENT ON TABLE plan_redemptions IS 'Auditable deductions from prepaid customer plans for completed appointments; reversal restores the balance.';
COMMENT ON COLUMN plan_redemptions.id IS 'Identity.';
COMMENT ON COLUMN plan_redemptions.plan_id IS 'The prepaid plan that paid for the appointment.';
COMMENT ON COLUMN plan_redemptions.appointment_id IS 'The completed appointment; only one active plan deduction per appointment.';
COMMENT ON COLUMN plan_redemptions.spa_id IS 'Owning spa for tenant isolation.';
COMMENT ON COLUMN plan_redemptions.amount IS 'Amount deducted in the unit recorded alongside it.';
COMMENT ON COLUMN plan_redemptions.unit IS 'hours or rupees, matching the plan type.';
COMMENT ON COLUMN plan_redemptions.created_at IS 'When the appointment consumed the plan.';
COMMENT ON COLUMN plan_redemptions.reversed_at IS 'When a deduction was reversed because the appointment left completed status.';
