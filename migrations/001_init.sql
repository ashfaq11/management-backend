-- Spa Manager schema. Applied once by app/migrate.py inside the configured schema (default "spa").
-- Needed so the database itself can refuse a therapist being booked twice at the same time.
CREATE EXTENSION IF NOT EXISTS btree_gist;

CREATE TABLE users (
  id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  username      TEXT NOT NULL CHECK (length(btrim(username)) >= 3),
  password_hash TEXT NOT NULL,
  role          TEXT NOT NULL DEFAULT 'staff' CHECK (role IN ('admin', 'staff')),
  is_active     BOOLEAN NOT NULL DEFAULT TRUE,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX users_username_key ON users (lower(username));

-- Opaque bearer tokens: only the SHA-256 fingerprint is stored.
CREATE TABLE sessions (
  token_hash TEXT PRIMARY KEY,
  user_id    BIGINT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX sessions_user_idx ON sessions (user_id);
CREATE INDEX sessions_expires_idx ON sessions (expires_at);

CREATE TABLE customers (
  id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name       TEXT NOT NULL CHECK (length(btrim(name)) > 0),
  phone      TEXT NOT NULL CONSTRAINT customers_phone_key UNIQUE,
  email      TEXT,
  gender     TEXT,
  dob        DATE,
  address    TEXT,
  notes      TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX customers_name_idx ON customers (lower(name));

CREATE TABLE therapists (
  id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name              TEXT NOT NULL CHECK (length(btrim(name)) > 0),
  phone             TEXT,
  fixed_salary      NUMERIC(12,2) NOT NULL DEFAULT 0 CHECK (fixed_salary >= 0),
  incentive_percent NUMERIC(5,2)  NOT NULL DEFAULT 0 CHECK (incentive_percent BETWEEN 0 AND 100),
  is_active         BOOLEAN NOT NULL DEFAULT TRUE,
  joined_on         DATE,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE services (
  id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name         TEXT NOT NULL CHECK (length(btrim(name)) > 0),
  price        NUMERIC(12,2) NOT NULL CHECK (price >= 0),
  duration_min INTEGER NOT NULL DEFAULT 60 CHECK (duration_min > 0),
  is_active    BOOLEAN NOT NULL DEFAULT TRUE
);
CREATE UNIQUE INDEX services_name_key ON services (lower(name));

-- start_at is the spa's local wall-clock time (no zone). price is the amount agreed at booking
-- (a later change to the service's list price never touches it); payroll is computed from it.
CREATE TABLE appointments (
  id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  customer_id  BIGINT NOT NULL REFERENCES customers  (id),
  therapist_id BIGINT NOT NULL REFERENCES therapists (id),
  service_id   BIGINT NOT NULL REFERENCES services   (id),
  start_at     TIMESTAMP NOT NULL,
  duration_min INTEGER NOT NULL CHECK (duration_min > 0),
  end_at       TIMESTAMP GENERATED ALWAYS AS (start_at + make_interval(mins => duration_min)) STORED,
  price        NUMERIC(12,2) NOT NULL CHECK (price >= 0),
  status       TEXT NOT NULL DEFAULT 'booked' CHECK (status IN ('booked', 'completed', 'cancelled', 'no_show')),
  notes        TEXT,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- one therapist cannot have two live appointments that overlap (back-to-back is fine)
  CONSTRAINT appointments_no_overlap
    EXCLUDE USING gist (therapist_id WITH =, tsrange(start_at, end_at) WITH &&)
    WHERE (status IN ('booked', 'completed'))
);
CREATE INDEX appointments_start_idx     ON appointments (start_at);
CREATE INDEX appointments_therapist_idx ON appointments (therapist_id, start_at);
CREATE INDEX appointments_customer_idx  ON appointments (customer_id, start_at DESC);
