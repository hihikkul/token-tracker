-- token-tracker ranking server (Cloudflare D1)
CREATE TABLE IF NOT EXISTS groups (
  code TEXT PRIMARY KEY,           -- invite code, e.g. RICE-7K2Q
  name TEXT NOT NULL,
  created INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS members (
  id TEXT PRIMARY KEY,             -- random member id
  group_code TEXT NOT NULL,
  token_hash TEXT NOT NULL,        -- sha-256 of the member's secret token
  nick TEXT NOT NULL,
  plans TEXT NOT NULL DEFAULT '',
  joined INTEGER NOT NULL,
  updated INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_members_group ON members(group_code);
CREATE TABLE IF NOT EXISTS scores (
  member_id TEXT NOT NULL,
  period TEXT NOT NULL,            -- day | week | month
  window TEXT NOT NULL,            -- 2026-10-11 | 2026-W41 | 2026-10 (the sender's local calendar)
  api REAL NOT NULL,               -- API-equivalent spend in the period, USD
  sub REAL NOT NULL,               -- subscription share for the period, USD
  updated INTEGER NOT NULL,
  PRIMARY KEY (member_id, period)
);
