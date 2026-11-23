"""SQLite store. One row per billed model call, deduplicated by uid."""
import json
import sqlite3
import threading

from .paths import db_path

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    uid TEXT PRIMARY KEY,
    source TEXT NOT NULL,          -- claude | codex | gemini
    ts INTEGER NOT NULL,           -- unix seconds (UTC)
    model TEXT,
    project TEXT,
    session TEXT,
    input INTEGER DEFAULT 0,       -- uncached input tokens
    output INTEGER DEFAULT 0,      -- output tokens (reasoning included)
    cache_write INTEGER DEFAULT 0,
    cache_read INTEGER DEFAULT 0,
    reasoning INTEGER DEFAULT 0    -- informational, already inside output
);
CREATE INDEX IF NOT EXISTS idx_records_ts ON records(ts);
CREATE INDEX IF NOT EXISTS idx_records_src_ts ON records(source, ts);
CREATE INDEX IF NOT EXISTS idx_records_session ON records(session);
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    mtime REAL,
    size INTEGER,
    offset INTEGER DEFAULT 0,
    state TEXT
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

_lock = threading.RLock()


class DB:
    def __init__(self, path=None):
        self.path = str(path or db_path())
        self.conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        with _lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(SCHEMA)
            columns = {row['name'] for row in self.conn.execute('PRAGMA table_info(records)')}
            if 'account' not in columns:
                self.conn.execute('ALTER TABLE records ADD COLUMN account TEXT')
            if 'codex_base' not in columns:
                self.conn.execute('ALTER TABLE records ADD COLUMN codex_base INTEGER')
            self.conn.commit()
        self.record_generation = 0

    # ------------------------------------------------------------ records
    def upsert_records(self, rows):
        """rows: iterable of dicts. Same uid seen twice keeps the larger counts
        (Claude Code writes one message several times while it streams)."""
        # Codex deltas are reconciled against their previous cumulative reading by the
        # collector; a better baseline can legitimately make the stored counts smaller.
        counts = ', '.join(f'{k} = CASE WHEN excluded.codex_base IS NOT NULL THEN excluded.{k} '
                           f'ELSE max({k}, excluded.{k}) END'
                           for k in ('input', 'output', 'cache_write', 'cache_read', 'reasoning'))
        sql = f"""
        INSERT INTO records(uid, source, ts, model, project, session, account, codex_base,
                            input, output, cache_write, cache_read, reasoning)
        VALUES(:uid, :source, :ts, :model, :project, :session, :account, :codex_base,
               :input, :output, :cache_write, :cache_read, :reasoning)
        ON CONFLICT(uid) DO UPDATE SET
            source = excluded.source,
            codex_base = excluded.codex_base,
            {counts},
            model = CASE WHEN excluded.model IS NOT NULL AND excluded.model != ''
                         AND (excluded.model NOT IN ('gemini', 'codex')
                              OR model IS NULL OR model IN ('', 'gemini', 'codex'))
                         THEN excluded.model ELSE model END,
            account = CASE WHEN excluded.source = 'codex' AND uid LIKE 'c:%'
                           THEN coalesce(excluded.account, account) ELSE excluded.account END
        """
        rows = list(rows)
        if not rows:
            return 0
        for r in rows:
            for k in ("model", "project", "session", "account", "codex_base"):
                r.setdefault(k, None)
            for k in ("input", "output", "cache_write", "cache_read", "reasoning"):
                r[k] = int(r.get(k) or 0)
        with _lock:
            try:
                self.conn.executemany(sql, rows)
                self.conn.commit()
                self.record_generation += 1
            except Exception:
                self.conn.rollback()
                raise
        return len(rows)

    # -------------------------------------------------------------- files
    def get_file(self, path):
        with _lock:
            row = self.conn.execute("SELECT * FROM files WHERE path=?", (str(path),)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["state"] = json.loads(d["state"]) if d["state"] else {}
        return d

    def set_file(self, path, mtime, size, offset, state=None):
        with _lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO files(path, mtime, size, offset, state) VALUES(?,?,?,?,?)",
                (str(path), mtime, size, offset, json.dumps(state or {})),
            )
            self.conn.commit()

    # ----------------------------------------------------------------- kv
    def get_kv(self, key, default=None):
        with _lock:
            row = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_kv(self, key, value):
        with _lock:
            self.conn.execute("INSERT OR REPLACE INTO kv(key, value) VALUES(?,?)",
                              (key, json.dumps(value)))
            self.conn.commit()

    def query(self, sql, params=()):
        with _lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]
