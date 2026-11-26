-- Optional preferred payment method shown as a default at checkout; transaction methods remain independent.
ALTER TABLE customers ADD COLUMN preferred_payment_method TEXT;
ALTER TABLE customers ADD CONSTRAINT customers_preferred_payment_method_check
  CHECK (preferred_payment_method IS NULL OR preferred_payment_method IN ('cash','upi','card','bank_transfer','other'));

COMMENT ON COLUMN customers.preferred_payment_method IS 'Optional default payment method preference; each payment records its actual method independently.';
