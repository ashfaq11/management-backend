-- Many spas in one system. Every business row belongs to exactly one spa; admins and staff belong to one
-- spa and see only its data; a superadmin belongs to none and can open any spa. Existing data becomes spa 1.
CREATE TABLE spas (
  id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name       TEXT NOT NULL CHECK (length(btrim(name)) > 0),
  is_active  BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX spas_name_key ON spas (lower(name));
INSERT INTO spas (name) OVERRIDING SYSTEM VALUE SELECT 'My Spa';

-- users: superadmin has no spa; admin and staff have exactly one
ALTER TABLE users ADD COLUMN spa_id BIGINT REFERENCES spas (id);
UPDATE users SET spa_id = (SELECT min(id) FROM spas);
ALTER TABLE users DROP CONSTRAINT users_role_check;
ALTER TABLE users ADD CONSTRAINT users_role_check CHECK (role IN ('superadmin', 'admin', 'staff'));
ALTER TABLE users ADD CONSTRAINT users_spa_by_role CHECK ((role = 'superadmin') = (spa_id IS NULL));
CREATE INDEX users_spa_idx ON users (spa_id);

-- every business table gets its spa
ALTER TABLE customers ADD COLUMN spa_id BIGINT REFERENCES spas (id);
ALTER TABLE therapists ADD COLUMN spa_id BIGINT REFERENCES spas (id);
ALTER TABLE services ADD COLUMN spa_id BIGINT REFERENCES spas (id);
ALTER TABLE appointments ADD COLUMN spa_id BIGINT REFERENCES spas (id);
ALTER TABLE therapist_bonuses ADD COLUMN spa_id BIGINT REFERENCES spas (id);
UPDATE customers SET spa_id = (SELECT min(id) FROM spas);
UPDATE therapists SET spa_id = (SELECT min(id) FROM spas);
UPDATE services SET spa_id = (SELECT min(id) FROM spas);
UPDATE appointments SET spa_id = (SELECT min(id) FROM spas);
UPDATE therapist_bonuses SET spa_id = (SELECT min(id) FROM spas);
ALTER TABLE customers ALTER COLUMN spa_id SET NOT NULL;
ALTER TABLE therapists ALTER COLUMN spa_id SET NOT NULL;
ALTER TABLE services ALTER COLUMN spa_id SET NOT NULL;
ALTER TABLE appointments ALTER COLUMN spa_id SET NOT NULL;
ALTER TABLE therapist_bonuses ALTER COLUMN spa_id SET NOT NULL;

-- phone numbers and service names are unique within a spa, not across spas
ALTER TABLE customers DROP CONSTRAINT customers_phone_key;
ALTER TABLE customers ADD CONSTRAINT customers_phone_key UNIQUE (spa_id, phone);
DROP INDEX services_name_key;
CREATE UNIQUE INDEX services_name_key ON services (spa_id, lower(name));

CREATE INDEX customers_spa_idx ON customers (spa_id, lower(name));
CREATE INDEX therapists_spa_idx ON therapists (spa_id);
CREATE INDEX appointments_spa_start_idx ON appointments (spa_id, start_at);
CREATE INDEX therapist_bonuses_spa_idx ON therapist_bonuses (spa_id, on_date);

COMMENT ON TABLE spas IS 'Each spa (business) using the system. Its admins and staff see only its data; a superadmin sees every spa.';
COMMENT ON COLUMN spas.id IS 'Identity.';
COMMENT ON COLUMN spas.name IS 'Business name, shown in the app and on payslips and reports; unique ignoring case.';
COMMENT ON COLUMN spas.is_active IS 'False stops every login of this spa at once (superadmin only); data is kept.';
COMMENT ON COLUMN spas.created_at IS 'When the spa was added.';
COMMENT ON COLUMN users.spa_id IS 'The spa an admin or staff login belongs to; NULL for a superadmin (users_spa_by_role).';
COMMENT ON COLUMN customers.spa_id IS 'The spa this customer belongs to; phone is unique within it (customers_phone_key).';
COMMENT ON COLUMN therapists.spa_id IS 'The spa this therapist works for.';
COMMENT ON COLUMN services.spa_id IS 'The spa offering this service; name is unique within it.';
COMMENT ON COLUMN appointments.spa_id IS 'The spa of the booking; always that of its customer, therapist and service.';
COMMENT ON COLUMN therapist_bonuses.spa_id IS 'The spa of the therapist.';
COMMENT ON COLUMN users.role IS 'superadmin (every spa, creates spas and their admins), admin (one spa, creates its staff) or staff.';
