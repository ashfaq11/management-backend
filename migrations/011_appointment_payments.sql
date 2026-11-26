-- Payments received against appointments and immutable refund records.
CREATE TABLE payments (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  spa_id BIGINT NOT NULL REFERENCES spas(id),
  appointment_id BIGINT NOT NULL REFERENCES appointments(id),
  customer_id BIGINT NOT NULL REFERENCES customers(id),
  amount NUMERIC(12,2) NOT NULL CHECK (amount > 0),
  method TEXT NOT NULL CHECK (method IN ('cash','upi','card','bank_transfer','other')),
  reference TEXT,
  note TEXT,
  received_by BIGINT REFERENCES users(id),
  received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX payments_spa_received_idx ON payments (spa_id, received_at DESC);
CREATE INDEX payments_appointment_idx ON payments (spa_id, appointment_id, received_at);

CREATE TABLE payment_refunds (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  spa_id BIGINT NOT NULL REFERENCES spas(id),
  payment_id BIGINT NOT NULL REFERENCES payments(id),
  amount NUMERIC(12,2) NOT NULL CHECK (amount > 0),
  reason TEXT,
  refunded_by BIGINT REFERENCES users(id),
  refunded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX payment_refunds_payment_idx ON payment_refunds (spa_id, payment_id, refunded_at);

COMMENT ON TABLE payments IS 'Amounts collected from customers against appointments; multiple partial payments are allowed.';
COMMENT ON COLUMN payments.id IS 'Identity and receipt number source.';
COMMENT ON COLUMN payments.spa_id IS 'Owning spa for tenant isolation.';
COMMENT ON COLUMN payments.appointment_id IS 'Appointment this payment settles.';
COMMENT ON COLUMN payments.customer_id IS 'Customer from whom payment was received.';
COMMENT ON COLUMN payments.amount IS 'Gross amount received in INR.';
COMMENT ON COLUMN payments.method IS 'Collection method: cash, UPI, card, bank transfer, or other.';
COMMENT ON COLUMN payments.reference IS 'Optional transaction reference, e.g. UPI reference or card slip.';
COMMENT ON COLUMN payments.note IS 'Optional note entered by the cashier.';
COMMENT ON COLUMN payments.received_by IS 'Login that recorded the payment.';
COMMENT ON COLUMN payments.received_at IS 'Local settlement timestamp for daily collection reports.';
COMMENT ON COLUMN payments.created_at IS 'When the payment record was created.';
COMMENT ON TABLE payment_refunds IS 'Immutable partial or full refunds against recorded appointment payments.';
COMMENT ON COLUMN payment_refunds.id IS 'Identity.';
COMMENT ON COLUMN payment_refunds.spa_id IS 'Owning spa for tenant isolation.';
COMMENT ON COLUMN payment_refunds.payment_id IS 'Original payment being refunded.';
COMMENT ON COLUMN payment_refunds.amount IS 'Refunded amount in INR; cumulative refunds cannot exceed the payment.';
COMMENT ON COLUMN payment_refunds.reason IS 'Optional reason for the refund.';
COMMENT ON COLUMN payment_refunds.refunded_by IS 'Admin login that authorised the refund.';
COMMENT ON COLUMN payment_refunds.refunded_at IS 'When the refund was recorded.';
