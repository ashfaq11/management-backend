-- What the customer actually paid for a plan or a top-up, so plan sales count as revenue.
ALTER TABLE customer_plans ADD COLUMN IF NOT EXISTS price_paid NUMERIC(12,2) CHECK (price_paid >= 0);
ALTER TABLE plan_topups ADD COLUMN IF NOT EXISTS price_paid NUMERIC(12,2) CHECK (price_paid >= 0);
-- rupee plans: paid what was loaded; hours plans sold before this have no known price (counted as 0)
UPDATE customer_plans p SET price_paid = p.purchased_amount
       - COALESCE((SELECT SUM(x.amount) FROM plan_topups x WHERE x.plan_id = p.id), 0)
 WHERE p.price_paid IS NULL AND p.plan_type = 'rupees';
UPDATE plan_topups x SET price_paid = x.amount FROM customer_plans p
 WHERE p.id = x.plan_id AND x.price_paid IS NULL AND p.plan_type = 'rupees';
COMMENT ON COLUMN customer_plans.price_paid IS 'Money received when the plan was sold (INR); counted as revenue on the sale date.';
COMMENT ON COLUMN plan_topups.price_paid IS 'Money received for this top-up (INR); counted as revenue on the top-up date.';
