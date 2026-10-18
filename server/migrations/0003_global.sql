-- Public ranking: one built-in group everyone may join, ordered by usage instead of ratio.
ALTER TABLE scores ADD COLUMN usage REAL NOT NULL DEFAULT 0;     -- API-equivalent spend of every tool, USD
ALTER TABLE scores ADD COLUMN tokens INTEGER NOT NULL DEFAULT 0; -- tokens of every tool
INSERT OR IGNORE INTO groups (code, name, created) VALUES ('GLOBAL', 'Ranking', 0);
CREATE INDEX IF NOT EXISTS idx_scores_period ON scores(period, window);
