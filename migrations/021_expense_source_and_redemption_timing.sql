-- Regular expenses record how they are paid; plan redemptions remember whether they were taken at booking.
ALTER TABLE recurring_expenses ADD COLUMN IF NOT EXISTS payment_method TEXT NOT NULL DEFAULT 'cash'
  CHECK (payment_method IN ('cash','upi','card','bank_transfer','other'));
ALTER TABLE plan_redemptions ADD COLUMN IF NOT EXISTS at_booking BOOLEAN NOT NULL DEFAULT false;
COMMENT ON COLUMN recurring_expenses.payment_method IS 'How this regular cost is paid each month.';
COMMENT ON COLUMN plan_redemptions.at_booking IS 'Taken when booking (stays if the visit is reopened); false = taken at completion (returned on reopen).';
