-- Bound unauthenticated enrollments without retaining client IP addresses.
CREATE TABLE IF NOT EXISTS enrollment_limits (
  client_hash TEXT NOT NULL,
  action TEXT NOT NULL,
  hour INTEGER NOT NULL,
  day INTEGER NOT NULL,
  hour_count INTEGER NOT NULL,
  day_count INTEGER NOT NULL,
  expires INTEGER NOT NULL,
  PRIMARY KEY (client_hash, action)
);
CREATE INDEX IF NOT EXISTS idx_enrollment_limits_expires ON enrollment_limits(expires);
