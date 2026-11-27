-- Spa subscriptions: a start and end date set by the superadmin (or a 30-day trial for self sign-ups).
-- After the end date the spa's users can still sign in, but only see the renewal screen.
ALTER TABLE spas ADD COLUMN IF NOT EXISTS plan_start DATE;
ALTER TABLE spas ADD COLUMN IF NOT EXISTS plan_end DATE;
ALTER TABLE spas ADD COLUMN IF NOT EXISTS is_trial BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE spas ADD COLUMN IF NOT EXISTS contact_phone TEXT;
ALTER TABLE spas ADD COLUMN IF NOT EXISTS contact_email TEXT;
COMMENT ON COLUMN spas.plan_start IS 'Subscription start date.';
COMMENT ON COLUMN spas.plan_end IS 'Subscription end date; after it the spa is locked to the renewal screen. NULL = no end.';
COMMENT ON COLUMN spas.is_trial IS 'true while on the free trial a self sign-up gets.';
COMMENT ON COLUMN spas.contact_phone IS 'Owner phone given at sign-up or by the superadmin.';
COMMENT ON COLUMN spas.contact_email IS 'Owner email given at sign-up or by the superadmin.';

-- Platform settings the superadmin edits: who to contact for renewal, trial length.
CREATE TABLE IF NOT EXISTS platform_settings (
  id SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  renewal_name TEXT,
  renewal_phone TEXT,
  renewal_email TEXT,
  renewal_message TEXT,
  trial_days INT NOT NULL DEFAULT 30 CHECK (trial_days BETWEEN 1 AND 365),
  allow_signup BOOLEAN NOT NULL DEFAULT true,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO platform_settings(id) VALUES (1) ON CONFLICT DO NOTHING;
COMMENT ON TABLE platform_settings IS 'One row of settings for the whole platform, edited by the superadmin.';
COMMENT ON COLUMN platform_settings.id IS 'Always 1.';
COMMENT ON COLUMN platform_settings.renewal_name IS 'Name shown on the renewal screen.';
COMMENT ON COLUMN platform_settings.renewal_phone IS 'Phone / WhatsApp shown on the renewal screen.';
COMMENT ON COLUMN platform_settings.renewal_email IS 'Email shown on the renewal screen.';
COMMENT ON COLUMN platform_settings.renewal_message IS 'Message shown on the renewal screen.';
COMMENT ON COLUMN platform_settings.trial_days IS 'Free trial length for self sign-ups.';
COMMENT ON COLUMN platform_settings.allow_signup IS 'Whether the public sign-up page is open.';
COMMENT ON COLUMN platform_settings.updated_at IS 'Last change.';
