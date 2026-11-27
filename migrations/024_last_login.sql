-- When each login last signed in, and how often.
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_login_at TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS login_count INT NOT NULL DEFAULT 0;
COMMENT ON COLUMN users.last_login_at IS 'Last successful sign-in.';
COMMENT ON COLUMN users.login_count IS 'Number of successful sign-ins.';
