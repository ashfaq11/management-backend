-- Top-ups add to an existing prepaid plan; each one is kept so sales reports count it in its own period.
CREATE TABLE plan_topups (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  plan_id BIGINT NOT NULL REFERENCES customer_plans(id),
  spa_id BIGINT NOT NULL REFERENCES spas(id),
  amount NUMERIC(12,2) NOT NULL CHECK (amount > 0),
  note TEXT,
  created_by BIGINT REFERENCES users(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX plan_topups_spa_time_idx ON plan_topups (spa_id, created_at);
CREATE INDEX plan_topups_plan_idx ON plan_topups (plan_id, created_at);
COMMENT ON TABLE plan_topups IS 'Amounts added to an existing prepaid plan after it was sold.';
COMMENT ON COLUMN plan_topups.id IS 'Identity.';
COMMENT ON COLUMN plan_topups.plan_id IS 'Plan that was topped up.';
COMMENT ON COLUMN plan_topups.spa_id IS 'Owning spa; same as the plan.';
COMMENT ON COLUMN plan_topups.amount IS 'Amount added, in the plan unit (hours or rupees).';
COMMENT ON COLUMN plan_topups.note IS 'Optional note, e.g. payment details.';
COMMENT ON COLUMN plan_topups.created_by IS 'Login that added the top-up.';
COMMENT ON COLUMN plan_topups.created_at IS 'When the top-up was added.';
