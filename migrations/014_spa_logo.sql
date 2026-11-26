-- Each spa owns its own optional brand logo. The UI stores a normalized image data URL.
ALTER TABLE spas ADD COLUMN logo_data_url TEXT;
COMMENT ON COLUMN spas.logo_data_url IS 'Optional PNG, JPEG or WebP logo data URL for this spa; independent of other spas.';
