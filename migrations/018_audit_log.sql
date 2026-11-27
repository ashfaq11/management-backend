-- Who changed what: one row per successful change (create, edit, delete, status, refund, payment, login...).
CREATE TABLE audit_log (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  at TIMESTAMPTZ NOT NULL DEFAULT now(),
  spa_id BIGINT REFERENCES spas(id),
  user_id BIGINT REFERENCES users(id),
  username TEXT,
  role TEXT,
  action TEXT NOT NULL,
  method TEXT NOT NULL,
  path TEXT NOT NULL,
  status SMALLINT NOT NULL,
  details JSONB,
  ip TEXT
);
CREATE INDEX audit_log_spa_at_idx ON audit_log (spa_id, at DESC);
CREATE INDEX audit_log_user_at_idx ON audit_log (user_id, at DESC);
COMMENT ON TABLE audit_log IS 'Append-only record of every successful change made through the API; never edited or deleted by the app.';
COMMENT ON COLUMN audit_log.id IS 'Identity.';
COMMENT ON COLUMN audit_log.at IS 'When the change happened.';
COMMENT ON COLUMN audit_log.spa_id IS 'Spa the change was made in (NULL for superadmin-wide actions).';
COMMENT ON COLUMN audit_log.user_id IS 'Login that made the change.';
COMMENT ON COLUMN audit_log.username IS 'Username at the time, kept even if the login is renamed.';
COMMENT ON COLUMN audit_log.role IS 'Role at the time (superadmin, admin, staff).';
COMMENT ON COLUMN audit_log.action IS 'Readable action, e.g. "Refund payment", "Edit service".';
COMMENT ON COLUMN audit_log.method IS 'HTTP method (POST, PUT, PATCH, DELETE).';
COMMENT ON COLUMN audit_log.path IS 'API path, with record ids.';
COMMENT ON COLUMN audit_log.status IS 'HTTP status returned.';
COMMENT ON COLUMN audit_log.details IS 'Request body with passwords and tokens removed.';
COMMENT ON COLUMN audit_log.ip IS 'Client address as seen by the API.';
