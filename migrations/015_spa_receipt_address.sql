-- Optional business address printed beneath the spa logo/name on customer receipts.
ALTER TABLE spas ADD COLUMN address TEXT;
COMMENT ON COLUMN spas.address IS 'Optional business address displayed on customer receipts.';
