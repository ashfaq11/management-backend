-- Plan packages: the plans a spa sells, at fixed prices, set up by an admin. Staff assign a package to a customer.
CREATE TABLE plan_packages (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  spa_id BIGINT NOT NULL REFERENCES spas(id),
  name TEXT NOT NULL CHECK (length(btrim(name)) > 0),
  plan_type TEXT NOT NULL CHECK (plan_type IN ('hours', 'rupees')),
  amount NUMERIC(12,2) NOT NULL CHECK (amount > 0),
  price NUMERIC(12,2) NOT NULL CHECK (price >= 0),
  validity_days INT CHECK (validity_days > 0),
  is_active BOOLEAN NOT NULL DEFAULT true,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX plan_packages_spa_name_key ON plan_packages (spa_id, lower(name));
ALTER TABLE customer_plans ADD COLUMN IF NOT EXISTS package_id BIGINT REFERENCES plan_packages(id);
COMMENT ON TABLE plan_packages IS 'Plans a spa sells at a fixed price; staff assign them to customers.';
COMMENT ON COLUMN plan_packages.id IS 'Identity.';
COMMENT ON COLUMN plan_packages.spa_id IS 'Owning spa.';
COMMENT ON COLUMN plan_packages.name IS 'Package name, e.g. 10 Hours Gold.';
COMMENT ON COLUMN plan_packages.plan_type IS 'hours or rupees, like customer_plans.plan_type.';
COMMENT ON COLUMN plan_packages.amount IS 'Hours or rupees the customer gets.';
COMMENT ON COLUMN plan_packages.price IS 'What the customer pays (INR).';
COMMENT ON COLUMN plan_packages.validity_days IS 'Days the plan stays valid after it is assigned; NULL = no expiry.';
COMMENT ON COLUMN plan_packages.is_active IS 'Inactive packages cannot be assigned; plans already sold are unaffected.';
COMMENT ON COLUMN plan_packages.created_at IS 'When the package was created.';
COMMENT ON COLUMN customer_plans.package_id IS 'Package the plan was assigned from; NULL = custom plan made by an admin.';
