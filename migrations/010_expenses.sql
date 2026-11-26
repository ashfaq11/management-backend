-- Spa running costs: regular monthly ones (rent, electricity, salaries outside payroll...) and one-off spends.
CREATE TABLE recurring_expenses (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  spa_id BIGINT NOT NULL REFERENCES spas(id),
  name TEXT NOT NULL CHECK (length(btrim(name)) > 0),
  category TEXT NOT NULL DEFAULT 'Other',
  amount NUMERIC(12,2) NOT NULL CHECK (amount > 0),
  day_of_month SMALLINT NOT NULL DEFAULT 1 CHECK (day_of_month BETWEEN 1 AND 28),
  starts_on DATE NOT NULL,
  ends_on DATE,
  notes TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (ends_on IS NULL OR ends_on >= starts_on)
);
CREATE INDEX recurring_expenses_spa_idx ON recurring_expenses (spa_id, starts_on);

CREATE TABLE expenses (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  spa_id BIGINT NOT NULL REFERENCES spas(id),
  on_date DATE NOT NULL,
  category TEXT NOT NULL DEFAULT 'Other',
  amount NUMERIC(12,2) NOT NULL CHECK (amount > 0),
  note TEXT,
  created_by BIGINT REFERENCES users(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX expenses_spa_date_idx ON expenses (spa_id, on_date);

COMMENT ON TABLE recurring_expenses IS 'A cost charged every month on a fixed day (rent, electricity, internet...), counted in each month it is active.';
COMMENT ON COLUMN recurring_expenses.id IS 'Identity.';
COMMENT ON COLUMN recurring_expenses.spa_id IS 'Owning spa.';
COMMENT ON COLUMN recurring_expenses.name IS 'What the cost is, e.g. Shop rent.';
COMMENT ON COLUMN recurring_expenses.category IS 'Grouping for reports, e.g. Rent, Utilities, Supplies.';
COMMENT ON COLUMN recurring_expenses.amount IS 'Monthly amount in INR.';
COMMENT ON COLUMN recurring_expenses.day_of_month IS 'Day (1-28) it falls due each month.';
COMMENT ON COLUMN recurring_expenses.starts_on IS 'First date it applies; months before are not charged.';
COMMENT ON COLUMN recurring_expenses.ends_on IS 'Last date it applies; NULL = ongoing.';
COMMENT ON COLUMN recurring_expenses.notes IS 'Optional notes.';
COMMENT ON COLUMN recurring_expenses.created_at IS 'When it was added.';
COMMENT ON TABLE expenses IS 'One-off spa costs on a given date.';
COMMENT ON COLUMN expenses.id IS 'Identity.';
COMMENT ON COLUMN expenses.spa_id IS 'Owning spa.';
COMMENT ON COLUMN expenses.on_date IS 'Date the money was spent.';
COMMENT ON COLUMN expenses.category IS 'Grouping for reports, e.g. Supplies, Repairs, Marketing.';
COMMENT ON COLUMN expenses.amount IS 'Amount in INR.';
COMMENT ON COLUMN expenses.note IS 'What it was for.';
COMMENT ON COLUMN expenses.created_by IS 'Login that recorded it.';
COMMENT ON COLUMN expenses.created_at IS 'When it was recorded.';
