-- Keep legacy scores separate from the corrected full-period subscription basis.
ALTER TABLE scores ADD COLUMN calculation_version INTEGER NOT NULL DEFAULT 1;
