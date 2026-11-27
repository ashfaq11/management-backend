-- Spa's own WhatsApp message texts (reminder, receipt, top-up, hello); NULL keys use the app's defaults.
ALTER TABLE spas ADD COLUMN IF NOT EXISTS wa_templates JSONB NOT NULL DEFAULT '{}'::jsonb;
COMMENT ON COLUMN spas.wa_templates IS 'WhatsApp message templates set on the Settings page; missing keys use built-in texts.';
