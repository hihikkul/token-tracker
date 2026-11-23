"""Read the logs each tool already writes and turn them into usage records.

Claude Code : ~/.claude/projects/**/*.jsonl         (assistant lines with message.usage)
Codex       : ~/.codex/sessions/**/rollout-*.jsonl  (event_msg token_count, cumulative)
Gemini CLI  : ~/.gemini/tmp/*/chats/*.json          (messages[].tokens)
Antigravity : ~/.gemini/antigravity*/brain/*/...    (best effort; current builds log no token counts)

JSONL files are append-only, so they are read incrementally from the last offset.
"""
import hashlib
import json
import math
import os
import re
import time
from bisect import bisect
from datetime import datetime, timezone
from pathlib import Path

from . import paths, ripple, sources


def _tokens(value):
    """Accept numeric token strings, but never silently turn malformed usage into zero."""
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("invalid token count")
    number = value if isinstance(value, int) else float(value)
    if not math.isfinite(number) or number < 0 or number > 2**63 - 1 or number != int(number):
        raise ValueError("invalid token count")
    return int(number)


def _text(value):
    if value is None or isinstance(value, str):
        return value
    raise ValueError("invalid text field")


def parse_ts(v):
    """ISO string / epoch seconds / epoch ms -> unix seconds (int) or None."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return (int(v / 1000) if v > 1e11 else int(v)) if math.isfinite(v) else None
    if isinstance(v, str):
        s = v.strip()
        if s.isdigit():
            return parse_ts(int(s))
        try:
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            # trim sub-microsecond digits that fromisoformat rejects on older Pythons
            s = re.sub(r"(\.\d{6})\d+", r"\1", s)
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except ValueError:
            return None
    return None


def _project_name(cwd):
    if not cwd:
        return None
    return Path(str(cwd).replace("\\", "/")).name or str(cwd)


def _file_fingerprint(path, offset):
    with open(path, "rb") as f:
        st = os.fstat(f.fileno())
        first = f.read(min(4096, offset))
        f.seek(max(0, offset - 256))
        tail = f.read(min(256, offset))
    return {"ino": st.st_ino, "dev": st.st_dev,
            "first": hashlib.sha256(first).hexdigest(), "tail": hashlib.sha256(tail).hexdigest()}


def read_new_lines(db, path, force=False):
    """Stream complete JSON lines, resuming only when the saved file still matches.
    The caller consumes lines before committing its parser state; incomplete tails wait."""
    st = path.stat()
    rec = db.get_file(path)
    offset, state = 0, {}
    if rec and not force and st.st_size >= rec["offset"]:
        fingerprint = _file_fingerprint(path, rec["offset"])
        if rec["state"].get("_fingerprint") == fingerprint:
            if rec["size"] == st.st_size and rec["mtime"] == st.st_mtime:
                return None
            offset, state = rec["offset"], rec["state"]
    new_offset, nlines = offset, state.get("_lines", 0)
    result = {"state": state, "skipped": 0}

    def lines():
        nonlocal new_offset, nlines
        with open(path, "rb") as f:
            f.seek(offset)
            while True:
                raw = f.readline()
                if not raw.endswith(b"\n"):
                    break
                lineno = nlines
                nlines += 1
                new_offset = f.tell()
                if not raw.strip():
                    continue
                try:
                    obj = json.loads(raw)
                except (ValueError, UnicodeError):
                    result["skipped"] += 1
                    continue
                yield lineno, obj

    def commit(new_state):
        new_state = dict(new_state or {})
        new_state["_lines"] = nlines
        new_state["_fingerprint"] = _file_fingerprint(path, new_offset)
        db.set_file(path, st.st_mtime, st.st_size, new_offset, new_state)

    result.update(lines=lines(), commit=commit)
    return result


# ======================================================================= Claude
def _claude_dir_project(path):
    """Fallback when a line has no cwd: 'C--Users-me-quant-vault' -> 'quant-vault'."""
    for parent in path.parents:
        if parent.parent.name == "projects":
            name = parent.name
            m = re.match(r"^[A-Za-z]--Users-[^-]+-(.+)$", name) or re.match(r"^-home-[^-]+-(.+)$", name)
            return m.group(1) if m else name
    return path.parent.name


_OPENAI = re.compile(r"^(gpt|chatgpt|codex|o\d)([-.\d]|$)")


def claude_log_source(model):
    """Which product a turn in a Claude Code transcript really used.

    Proxies such as ClaudeRipple let GPT (on a ChatGPT/Codex plan), Gemini and other models answer
    inside Claude Code; the transcript then carries that model's real name. Those turns spend the
    other product's quota, not Claude's, so they are counted there."""
    m = (model or "").strip().lower()
    if "/" in m or "gpt-oss" in m:
        return sources.vendor_source(m)
    if not m or re.search(r'(^|[.])claude-', m) or m.split("-")[0] in ("opus", "sonnet", "haiku"):
        return "claude"
    if _OPENAI.search(m):
        return "codex"
    if m.startswith("gemini-"):
        return "gemini"
    return sources.vendor_source(m)


def codex_log_source(model, provider=None):
    """A Codex log is subscription usage only for a bare OpenAI-family model."""
    src = claude_log_source(model or "codex")
    if provider and str(provider).lower() not in ("openai", "chatgpt", "codex"):
        return sources.vendor_source(model or str(provider)) if src != "codex" else sources.vendor_source(str(provider))
    return "codex" if src == "codex" else sources.vendor_source(model)


def _deepseek_configured():
    from urllib.parse import urlparse
    def matches(url):
        return isinstance(url, str) and "deepseek" in (urlparse(url).hostname or "").lower()
    if matches(os.environ.get("ANTHROPIC_BASE_URL")):
        return True
    for root in paths.claude_roots():
        try:
            cfg = json.loads((root / "settings.json").read_text(encoding="utf-8"))
            if isinstance(cfg, dict) and isinstance(cfg.get("env"), dict) and matches(cfg["env"].get("ANTHROPIC_BASE_URL")):
                return True
        except (OSError, ValueError):
            pass
    return False


def _claude_turn_model(model, record, usage, deepseek):
    src = claude_log_source(model)
    if src == "claude" and model.lower().startswith("claude-") and deepseek:
        anthropic = str(record.get("requestId") or "").startswith("req_") or isinstance(usage.get("cache_creation"), dict)
        if not anthropic:
            return "deepseek", "deepseek-v4-pro" if model.lower().startswith("claude-opus") else "deepseek-flash"
    return src, model


def proxy_chatgpt_account():
    """The ChatGPT login that answers GPT turns proxied out of Claude Code.

    A running ClaudeRipple says which account it is sending with right now, which moves whenever one
    runs out. When it cannot be asked, its next pick is the login marked preferred, then the first
    usable one in chatgpt-accounts.json (skipping paused or signed-out ones), and only then the
    Codex CLI's login. Without ClaudeRipple, the Codex CLI's login is the account a ChatGPT-plan
    proxy would borrow. Only ids are read here."""
    active = ripple.active_account()
    if active:
        return active
    try:
        data = json.loads((paths.clauderipple_home() / "chatgpt-accounts.json").read_text(encoding="utf-8"))
        accounts = data.get("accounts", data) if isinstance(data, dict) else data
        usable = [a for a in accounts or [] if isinstance(a, dict) and a.get("accountId")
                  and not a.get("paused") and not a.get("needsReauth")]
        if usable:
            return max(usable, key=lambda a: bool(a.get("preferred")))["accountId"]
    except (OSError, ValueError, AttributeError):
        pass
    try:
        tokens = json.loads((paths.codex_root() / "auth.json").read_text(encoding="utf-8")).get("tokens") or {}
        return tokens.get("account_id") or None
    except (OSError, ValueError, AttributeError):
        return None


def _router_lookup(timeline=None):
    if timeline is None:
        timeline = ripple.account_timeline()
    times = [t for t, _ in timeline]

    def lookup(ts):
        i = bisect(times, ts)
        nearby = timeline[max(0, i - 1):i + 1]
        if nearby:
            at, account = min(nearby, key=lambda event: abs(event[0] - ts))
            if abs(at - ts) <= ROUTER_MATCH_SEC:
                return account
        return None
    return lookup


def _split_claude_vendors(db):
    """Re-run the upstream Claude split, then repair all old vendor buckets by name.
    Each migration has its own marker; masked names still need transcript evidence."""
    split = db.get_kv("claude_vendor_split") != 2
    vendors = db.get_kv("vendor_split_v2") != 1
    if not split and not vendors:
        return
    rows = db.query("SELECT uid, source, model FROM records WHERE source IN ('other','claude','codex','gemini')")
    from .db import _lock
    with _lock:
        try:
            for r in rows:
                if not vendors and r['source'] != 'claude':
                    continue
                if r['model'] in (None, '', 'gemini', 'codex'):
                    continue  # A fallback name cannot establish a different provider.
                src = codex_log_source(r['model']) if r['uid'].startswith('x:') else claude_log_source(r['model'])
                db.conn.execute("UPDATE records SET source = ?, account = CASE WHEN ? = 'codex' THEN account ELSE NULL END WHERE uid = ?",
                                (src, src, r['uid']))
            db.conn.commit()
        except Exception:
            db.conn.rollback()
            raise
    if split:
        db.set_kv("claude_vendor_split", 2)
    if vendors:
        db.set_kv("vendor_split_v2", 1)


def _is_usage_placeholder(source, usage):
    """A proxy's first line for a turn holds only its estimate of the whole prompt as input, before
    the model answers: no cache split and no output. Proxied turns are billed mostly as cache reads,
    so counting it as input can overstate a turn a hundred times. Claude's own first line is exact."""
    return source != "claude" and not any(_tokens(usage.get(key)) for key in (
        "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))


def _repair_proxy_usage(db, deepseek):
    """Replace stored proxy estimates with real usage, dropping estimate-only turns.
    Version 2 also repairs newly classified vendors and masked DeepSeek transcripts."""
    if db.get_kv("claude_proxy_usage_repaired") == 2:
        return
    final, classifications = {}, {}
    for root in paths.claude_project_dirs():
        for path in root.rglob("*.jsonl"):
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    for lineno, line in enumerate(f):
                        if not line.endswith('\n') or '"usage"' not in line:
                            continue
                        try:
                            o = json.loads(line)
                            if not isinstance(o, dict) or o.get("type") != "assistant":
                                continue
                            msg = o.get("message") or {}
                            u, model = msg.get("usage"), msg.get("model")
                            if not u or not isinstance(u, dict) or not model or not isinstance(model, str) or model == "<synthetic>":
                                continue
                            if parse_ts(o.get("timestamp")) is None:
                                continue
                            counts = tuple(_tokens(u.get(key)) for key in (
                                "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens"))
                            src, model = _claude_turn_model(model, o, u, deepseek)
                            if src == "claude":
                                continue
                            mid = msg.get("id")
                            uid = f"c:{mid}:{o.get('requestId')}" if mid else f"c:{path}:{lineno}"
                            classifications[uid] = (src, model, src, uid)
                            if _is_usage_placeholder(src, u):
                                continue
                            if uid not in final or counts[3] >= final[uid][3]:
                                final[uid] = (*counts, uid)
                        except (ValueError, TypeError, AttributeError, OverflowError):
                            continue
            except OSError:
                continue
    from .db import _lock
    with _lock:
        try:
            db.conn.executemany("UPDATE records SET source = ?, model = ?, "
                                "account = CASE WHEN ? = 'codex' THEN account ELSE NULL END WHERE uid = ?",
                                list(classifications.values()))
            db.conn.executemany("UPDATE records SET input = ?, cache_read = ?, cache_write = ?, output = ? "
                                "WHERE uid = ? AND source <> 'claude'", list(final.values()))
            db.conn.execute("DELETE FROM records WHERE uid LIKE 'c:%' AND source <> 'claude' "
                            "AND output = 0 AND cache_read = 0 AND cache_write = 0")
            db.conn.commit()
        except Exception:
            db.conn.rollback()
            raise
    db.set_kv("claude_proxy_usage_repaired", 2)


def scan_claude(db, report):
    n_files = n_rows = skipped = 0
    _split_claude_vendors(db)
    deepseek = _deepseek_configured()
    _repair_proxy_usage(db, deepseek)
    proxy_acct = router = None
    now = time.time()
    # Accounts already stored are left alone: many came from router log lines that have since
    # rotated away, so they cannot be re-checked now. Only newly read rows use the rule below.
    for root in paths.claude_project_dirs():
        for path in root.rglob("*.jsonl"):
            n_files += 1
            try:
                res = read_new_lines(db, path)
                if res is None:
                    continue
                rows = []
                for lineno, o in res["lines"]:
                    try:
                        if not isinstance(o, dict):
                            raise ValueError("invalid record")
                        if o.get("type") != "assistant":
                            continue
                        msg = o.get("message") or {}
                        u, model = msg.get("usage"), msg.get("model")
                        if not u or not model or model == "<synthetic>":
                            continue
                        if not isinstance(u, dict) or not isinstance(model, str):
                            raise ValueError("invalid usage")
                        ts = parse_ts(o.get("timestamp"))
                        if ts is None:
                            raise ValueError("invalid timestamp")
                        counts = {key: _tokens(u.get(field)) for key, field in (
                            ("input", "input_tokens"), ("output", "output_tokens"),
                            ("cache_write", "cache_creation_input_tokens"), ("cache_read", "cache_read_input_tokens"))}
                        mid, rid = msg.get("id"), o.get("requestId")
                        uid = f"c:{mid}:{rid}" if mid else f"c:{path}:{lineno}"
                        src, model = _claude_turn_model(model, o, u, deepseek)
                        if _is_usage_placeholder(src, u):
                            continue
                        account = None
                        if src == 'codex':
                            router = router or _router_lookup()
                            account = router(ts)
                            if not account and 0 <= now - ts <= 900:
                                if proxy_acct is None:
                                    proxy_acct = proxy_chatgpt_account() or ''
                                account = proxy_acct or None
                        rows.append({"uid": uid, "source": src, "ts": ts, "model": model,
                                     "account": account,
                                     "project": _project_name(o.get("cwd")) or _claude_dir_project(path),
                                     "session": _text(o.get("sessionId")) or path.stem, **counts})
                    except (ValueError, TypeError, AttributeError, OverflowError):
                        skipped += 1
                n_rows += db.upsert_records(rows)
                skipped += res["skipped"]
                res["commit"](res["state"])
            except (OSError, ValueError, TypeError):
                skipped += 1
    report["claude"] = {"files": n_files, "new_records": n_rows, "skipped": skipped,
                        "dirs": [str(p) for p in paths.claude_project_dirs(existing_only=False)]}


# ======================================================================== Codex
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def _codex_limits_snapshot(rl, ts):
    if not isinstance(rl, dict):
        return None
    out = {"ts": ts, "windows": [], "limit_id": rl.get("limit_id") or "codex"}
    for key in ("primary", "secondary"):
        w = rl.get(key)
        if not isinstance(w, dict) or w.get("used_percent") is None:
            continue
        try:
            resets_at = parse_ts(w.get("resets_at"))
            if resets_at is None and w.get("resets_in_seconds") is not None:
                resets_at = ts + _tokens(w["resets_in_seconds"])
            used = float(w["used_percent"])
            if not math.isfinite(used):
                continue
            out["windows"].append({
                "key": key, "used_percent": used,
                "window_minutes": _tokens(w.get("window_minutes")), "resets_at": resets_at,
            })
        except (ValueError, TypeError, OverflowError):
            continue
    if rl.get("plan_type"):
        out["plan"] = rl["plan_type"]
    if rl.get("rate_limit_reached_type"):
        out["reached"] = rl["rate_limit_reached_type"]
    return out if out["windows"] else None


def _quota_resets(snap):
    return {w.get("window_minutes"): w.get("resets_at") for w in snap.get("windows", [])
            if w.get("resets_at") and w.get("window_minutes")}


def same_quota(a, b):
    """Two snapshots show one quota when their longest shared window resets at the same moment."""
    ra, rb = _quota_resets(a), _quota_resets(b)
    common = [m for m in ra if m in rb]
    return bool(common) and abs(ra[max(common)] - rb[max(common)]) <= 120


def _quota_owner(snap, live):
    """The ClaudeRipple account whose live quota this logged snapshot shows.

    Codex logs name the login that created a session, but ClaudeRipple may answer its requests with
    another account. The quota in the log is the one that really answered, so it identifies it."""
    owners = {s["account"] for s in live if same_quota(snap, s)}
    return owners.pop() if len(owners) == 1 else None


def _limits_key(snap, acct):
    # Distinct account IDs remain distinct even when both use the same plan.
    # Observed mid-session plan changes are passed here with no verified account ID.
    who = acct or (f"plan:{snap['plan']}" if snap.get("plan") else "default")
    return f"{who}:{snap['limit_id']}"


def _keep_limits(accounts, snap, acct, plans, learn):
    """Store the newest snapshot per quota. Returns True once this file taught a plan.

    Newer Codex builds often leave plan_type out. Such snapshots stay keyed by login and get
    a plan_guess for their label; the guess comes from the first labelled snapshot of a
    session, which belongs to the login that started it (a mid-session switch comes later)."""
    matched = None
    if not snap.get('plan') or acct is None:
        def weekly_reset(value):
            windows = [w for w in value.get('windows', []) if (w.get('window_minutes') or 0) >= 1440 and w.get('resets_at')]
            return max(windows, key=lambda w:w['window_minutes'])['resets_at'] if windows else None
        reset = weekly_reset(snap)
        candidates = [s for s in accounts.values() if s.get('account') and reset and weekly_reset(s)
                      and abs(reset-weekly_reset(s)) <= 120
                      and (not snap.get('plan') or snap['plan'] in (s.get('plan'),s.get('plan_guess')))]
        owners = {s['account'] for s in candidates}
        if len(owners) == 1:
            matched = max(candidates,key=lambda s:s['ts'])
            acct = matched['account']
    who = acct or "default"
    if snap.get("plan"):
        if learn:
            plans[who] = snap["plan"]
            learn = False
    elif matched or plans.get(who):
        snap["plan_guess"] = (matched.get('plan') or matched.get('plan_guess')) if matched else plans[who]
    key = _limits_key(snap, acct)
    if snap["ts"] >= (accounts.get(key) or {}).get("ts", 0):
        snap["account"] = acct
        accounts[key] = snap
    return learn


LIMITS_VERSION = 7
ROUTER_MATCH_SEC = 6 * 3600


def _assign_accounts_from_router(db):
    """One-off: ClaudeRipple's router log names the account behind each Codex request. Records stored
    under a session's creator, or under the first login on ClaudeRipple's list, take the account seen
    closest in time. Its GPT turns from Claude Code are not logged per account but use the same pick.
    Records with no log line nearby keep what they had."""
    if db.get_kv("codex_router_accounts") == 1:
        return
    timeline = ripple.account_timeline()
    if timeline:
        lookup = _router_lookup(timeline)
        updates = [(acct, r['uid']) for r in db.query(
            "SELECT uid, ts FROM records WHERE source = 'codex' AND ts >= ?",
            (timeline[0][0] - ROUTER_MATCH_SEC,)) if (acct := lookup(r['ts']))]
        from .db import _lock
        with _lock:
            db.conn.executemany("UPDATE records SET account = ? WHERE uid = ?", updates)
            db.conn.commit()
    db.set_kv("codex_router_accounts", 1)


def _rebuild_codex_limits(db):
    """One-off re-read of the last 8 days of Codex logs after the limits keying changed."""
    accounts, plans, cutoff = {}, {}, time.time() - 8 * 86400
    live = ripple.chatgpt_snapshots()
    files = []
    for d in paths.codex_session_dirs():
        for path in d.rglob("*.jsonl"):
            try:
                if path.stat().st_mtime >= cutoff:
                    files.append(path)
            except OSError:
                pass
    for path in sorted(files, key=lambda x: x.name):
        try:
            f = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        acct, learn, identity_plan, model, provider = None, True, None, None, None
        with f:
            for line in f:
                try:
                    o = json.loads(line)
                    p = o.get("payload") or {}
                    if o.get("type") in ("session_meta", "turn_context"):
                        acct = p.get("creator_account_id") or acct
                        model = p.get("model") or model
                        provider = p.get("model_provider") or provider
                    elif o.get("type") == "event_msg" and p.get("type") == "token_count":
                        if codex_log_source(model, provider) != "codex":
                            continue
                        ts = parse_ts(o.get("timestamp"))
                        snap = _codex_limits_snapshot(p.get("rate_limits"), ts) if ts else None
                        if snap:
                            identity_plan = identity_plan or snap.get('plan')
                            owner = None if snap.get('plan') and identity_plan != snap['plan'] else acct
                            learn = _keep_limits(accounts, snap, _quota_owner(snap, live) or owner, plans, learn)
                except (ValueError, TypeError, AttributeError, OverflowError):
                    continue
    db.set_kv("codex_limits_by_account", accounts)
    db.set_kv("codex_plan_by_account", plans)
    db.set_kv("codex_limits_version", LIMITS_VERSION)


def _codex_history(db, sid):
    """Find known UIDs and timestamps by reset generation, never cross-file baselines."""
    history = {}
    prefix = f"x:{sid}:"
    for row in db.query("SELECT * FROM records WHERE session = ? OR (uid >= ? AND uid < ?)",
                        (sid, prefix, prefix + '￿')):
        match = re.fullmatch(re.escape(f"x:{sid}:") + r"(\d+)(?::g(\d+))?", row['uid'])
        if match:
            total, generation = int(match[1]), int(match[2] or 0)
            history.setdefault(generation, {})[total] = {'row': row}
    return history


def scan_codex(db, report):
    """Delta each file's counter independently; deduplicate UIDs across copies and resets."""
    n_files = n_rows = skipped = 0
    _split_claude_vendors(db)
    backfill = db.get_kv('codex_record_accounts_version') != 2
    repair_deltas = db.get_kv('codex_deltas_v2') != 1
    if db.get_kv("codex_limits_version") != LIMITS_VERSION:
        _rebuild_codex_limits(db)
    accounts = db.get_kv("codex_limits_by_account") or {}
    plans = db.get_kv("codex_plan_by_account") or {}
    live = ripple.chatgpt_snapshots()
    histories = {}
    for d in paths.codex_session_dirs():
        for path in d.rglob("*.jsonl"):
            n_files += 1
            try:
                res = read_new_lines(db, path, force=backfill or repair_deltas)
                if res is None:
                    continue
                state = dict(res["state"])
                match = _UUID.search(path.name)
                sid = state.get("sid") or (match[0] if match else path.stem)
                model, cwd, acct = state.get("model"), state.get("cwd"), state.get("acct")
                provider = state.get('provider')
                learn = state.get("learn", True)
                identity_plan = state.get('identity_plan')
                conflict = state.get('account_conflict', False)
                served = state.get('served')  # the account that really answered, when ClaudeRipple shows it
                prev, generation = state.get("prev") or {}, state.get('generation', 0)
                rows = []
                for _, o in res["lines"]:
                    try:
                        typ, p = o.get("type"), o.get("payload") or {}
                        if not isinstance(p, dict):
                            raise ValueError("invalid payload")
                        if typ in ("session_meta", "turn_context"):
                            fields = {key: _text(p.get(key)) for key in
                                      ('id', 'session_id', 'cwd', 'model', 'model_provider', 'creator_account_id')}
                            if typ == 'session_meta':
                                sid = fields['id'] or fields['session_id'] or sid
                            cwd, model = fields['cwd'] or cwd, fields['model'] or model
                            provider = fields['model_provider'] or provider
                            acct = fields['creator_account_id'] or acct
                            continue
                        if typ != "event_msg" or p.get("type") != "token_count":
                            continue
                        if model is not None and not isinstance(model, str):
                            raise ValueError("invalid model")
                        ts = parse_ts(o.get("timestamp"))
                        if ts is None:
                            raise ValueError("invalid timestamp")
                        src = codex_log_source(model, provider)
                        snap = _codex_limits_snapshot(p.get("rate_limits"), ts) if src == 'codex' else None
                        if snap:
                            identity_plan = identity_plan or snap.get('plan')
                            if snap.get('plan'):
                                conflict = snap['plan'] != identity_plan
                            served = _quota_owner(snap, live) or served
                            learn = _keep_limits(accounts, snap, served or (None if conflict else acct), plans, learn)
                            if acct and snap.get('account') and snap['account'] != acct:
                                conflict = True
                        info = p.get("info") or {}
                        tot = info.get("total_token_usage")
                        if tot is None:
                            continue
                        if not isinstance(tot, dict):
                            raise ValueError("invalid usage")
                        cur = {k: _tokens(tot.get(k)) for k in
                               ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
                                "output_tokens", "reasoning_output_tokens", "total_tokens")}
                        if cur["total_tokens"] < prev.get("total_tokens", 0):
                            prev, generation = {}, generation + 1
                        if sid not in histories:
                            histories[sid] = _codex_history(db, sid)
                        history = histories[sid]
                        if not prev:
                            # A partial archive may begin after a reset; identical event timestamps
                            # identify its generation without relying on the archive's path.
                            for gen, events in history.items():
                                event = events.get(cur['total_tokens'])
                                if event and event['row']['ts'] == ts:
                                    generation = gen
                                    break
                        events = history.setdefault(generation, {})
                        known = events.get(cur['total_tokens'])
                        # Fork rollouts can share sid while restarting an independent counter.
                        delta = {k: cur[k] - prev.get(k, 0) for k in cur}
                        base = prev.get('total_tokens', 0)
                        prev = cur
                        if delta["total_tokens"] <= 0 and not known:
                            continue
                        cached = max(0, delta["cached_input_tokens"])
                        cwrite = max(0, delta["cache_write_input_tokens"])
                        counts = {"input": max(0, delta["input_tokens"] - cached - cwrite),
                                  "output": max(0, delta["output_tokens"]), "cache_read": cached,
                                  "cache_write": cwrite, "reasoning": max(0, delta["reasoning_output_tokens"])}
                        if known:
                            old = known['row']
                            old_base = old['codex_base']
                            if old_base is None:  # a delta saved before baselines were recorded
                                old_base = cur['total_tokens'] - sum(old[k] for k in ('input', 'cache_read', 'cache_write', 'output'))
                            if delta['total_tokens'] <= 0 or base <= old_base:
                                base, counts = old_base, {key: old[key] for key in counts}
                        uid = f"x:{sid}:{cur['total_tokens']}" + (f":g{generation}" if generation else "")
                        row = {"uid": uid, "source": src, "ts": ts, "model": model or "codex", 'codex_base': base,
                               "project": _project_name(cwd), "session": sid,
                               'account': (served or (None if conflict else acct)) if src == 'codex' else None, **counts}
                        rows.append(row)
                        events[cur['total_tokens']] = {'row': row}
                    except (ValueError, TypeError, AttributeError, OverflowError):
                        skipped += 1
                n_rows += db.upsert_records(rows)
                skipped += res['skipped']
                res["commit"]({"sid": sid, "model": model, "cwd": cwd, "acct": acct, "prev": prev, "learn": learn,
                               'provider': provider, 'generation': generation,
                               'identity_plan': identity_plan, 'account_conflict': conflict, 'served': served})
            except (OSError, ValueError, TypeError):
                skipped += 1
    if accounts:
        db.set_kv("codex_limits_by_account", accounts)
        db.set_kv("codex_plan_by_account", plans)
    if repair_deltas:
        db.set_kv('codex_deltas_v2', 1)
        db.set_kv('codex_router_accounts', 0)  # restore router evidence after replay
    db.set_kv('codex_record_accounts_version', 2)
    _assign_accounts_from_router(db)
    report["codex"] = {"files": n_files, "new_records": n_rows, "accounts": len(accounts), "skipped": skipped,
                       "dirs": [str(p) for p in paths.codex_session_dirs(existing_only=False)]}


# ======================================================================= Gemini
def _gemini_usage(d):
    """Return normalized token dict from a Gemini-style object, or None."""
    t = d.get("tokens")
    if isinstance(t, dict) and ("input" in t or "output" in t):
        inp, cached = _tokens(t.get("input")), _tokens(t.get("cached"))
        thoughts = _tokens(t.get("thoughts"))
        return {"input": max(0, inp - cached) + _tokens(t.get("tool")),
                "output": _tokens(t.get("output")) + thoughts,
                "cache_read": cached, "reasoning": thoughts}
    u = d.get("usageMetadata") or d.get("usage_metadata")
    if isinstance(u, dict) and ("promptTokenCount" in u or "candidatesTokenCount" in u):
        inp = _tokens(u.get("promptTokenCount"))
        cached = _tokens(u.get("cachedContentTokenCount"))
        thoughts = _tokens(u.get("thoughtsTokenCount"))
        return {"input": max(0, inp - cached) + _tokens(u.get("toolUsePromptTokenCount")),
                "output": _tokens(u.get("candidatesTokenCount")) + thoughts,
                "cache_read": cached, "reasoning": thoughts}
    return None


def _walk(o, parent_ts=None, parent_model=None, depth=0):
    """Yield (dict, ts, model) for every dict carrying token usage."""
    if depth > 12:
        return
    if isinstance(o, dict):
        ts = parse_ts(o.get("timestamp") or o.get("createTime") or o.get("time")) or parent_ts
        model = o.get("model") or o.get("modelVersion") or parent_model
        if any(key in o for key in ('tokens', 'usageMetadata', 'usage_metadata')):
            yield o, ts, model
        for v in o.values():
            if isinstance(v, (dict, list)):
                yield from _walk(v, ts, model, depth + 1)
    elif isinstance(o, list):
        for v in o:
            yield from _walk(v, parent_ts, parent_model, depth + 1)


def _gemini_files():
    root = paths.gemini_root()
    files = []
    tmp = root / "tmp"
    if tmp.exists():
        files += list(tmp.glob("*/chats/*.json"))
    # Antigravity keeps transcripts under brain/<id>/.system_generated/logs; current
    # versions write no token counts there, so these usually yield nothing.
    for sub in root.glob("antigravity*"):
        if sub.is_dir():
            files += list(sub.glob("brain/*/.system_generated/logs/transcript_full.jsonl"))
            files += list(sub.glob('log/**/*.jsonl'))
    for extra in paths.load_config().get("extra_paths", {}).get("gemini", []):
        p = Path(extra)
        if p.exists():
            files += [q for q in p.rglob("*") if q.suffix in (".json", ".jsonl")]
    return files


def _gemini_message_key(d, ts, model, occurrences):
    mid = d.get('id')
    if mid is not None:
        return str(mid)
    immutable = {key: d[key] for key in ('timestamp', 'createTime', 'time', 'role', 'type', 'content', 'text', 'parts') if key in d}
    immutable.update(timestamp=ts, model=model)
    digest = hashlib.sha256(json.dumps(immutable, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()
    occurrence = occurrences.get(digest, 0)
    occurrences[digest] = occurrence + 1
    return f"anon:{digest}:{occurrence}"


def _gemini_uid(session, mid):
    return 'gm:' + json.dumps([session, mid], ensure_ascii=False, separators=(',', ':'))


def _migrate_gemini_uids(db):
    if db.get_kv('gemini_message_uids') == 1:
        return False
    # Old UIDs contain the complete relative (or absolute) path, then the message id.
    # A session equal to the filename may be the old parser's fallback, not an ID.
    # Leave those rows path-keyed until a transcript supplies an explicit sessionId.
    rows, old = [], []
    for row in db.query("SELECT * FROM records WHERE uid GLOB 'g:*' ORDER BY ts"):
        match = re.fullmatch(r'g:(.+?\.jsonl?):(.*)', row['uid'], re.S)
        if not match or not row['session'] or match[2].startswith('anon:'):
            continue
        if Path(match[1].replace('\\', '/')).stem == row['session']:
            continue
        old.append((row['uid'],))
        row['uid'] = _gemini_uid(row['session'], match[2])
        rows.append(row)
    db.upsert_records(rows)
    from .db import _lock
    with _lock, db.conn:
        db.conn.executemany('DELETE FROM records WHERE uid=?', old)
    return True


def scan_gemini(db, report):
    n_files = n_rows = skipped = skipped_large = 0
    root = paths.gemini_root()
    migrate = _migrate_gemini_uids(db)
    legacy = {}
    for path in _gemini_files():
        n_files += 1
        try:
            st = path.stat()
            res = None
            if path.suffix == '.jsonl':
                res = read_new_lines(db, path, force=migrate)
                if res is None:
                    continue
                objs = (obj for _, obj in res['lines'])
                state = dict(res['state'])
            else:
                if st.st_size > 80 * 1024 * 1024:
                    skipped_large += 1
                    continue
                rec = db.get_file(path)
                if not migrate and rec and rec['mtime'] == st.st_mtime and rec['size'] == st.st_size:
                    continue
                objs = [json.loads(path.read_text(encoding='utf-8', errors='replace'))]
                state = {}
            rel = str(path.relative_to(root)) if root in path.parents else str(path)
            kind = "antigravity" if "antigravity" in rel.lower() else "gemini-cli"
            session = state.get('session_id')
            retired = []
            occurrences = state.get('occurrences') or {}
            rows = []
            for obj in objs:
                if not isinstance(obj, (dict, list)):
                    skipped += 1
                    continue
                if isinstance(obj, dict):
                    session = obj.get('sessionId') or session
                for d, ts, model in _walk(obj):
                    try:
                        u = _gemini_usage(d)
                        if not u or (model is not None and not isinstance(model, str)):
                            raise ValueError('invalid usage')
                        mid = _gemini_message_key(d, ts, model, occurrences)
                        uid = f'g:{rel}:{mid}'
                        if session and d.get('id') is not None:
                            uid = _gemini_uid(session, mid)
                            if session not in legacy:
                                legacy[session] = {}
                                for old in db.query("SELECT * FROM records WHERE session=? AND uid GLOB 'g:*'", (session,)):
                                    match = re.fullmatch(r'g:(.+?\.jsonl?):(.*)', old['uid'], re.S)
                                    if match:
                                        legacy[session].setdefault(match[2], []).append((match[1], old))
                            for old_path, old in legacy[session].get(mid, ()):
                                if old_path == rel or not (root / old_path).exists():
                                    rows.append(dict(old, uid=uid))
                                    retired.append((old['uid'],))
                        rows.append({
                            "uid": uid, "source": 'antigravity' if kind == 'antigravity' else 'gemini',
                            "ts": ts or int(st.st_mtime), "model": model or "gemini",
                            "project": kind if kind == "antigravity" else (path.parent.parent.name[:8] if path.parent.name == "chats" else kind),
                            "session": session or path.stem, "cache_write": 0, **u,
                        })
                        if len(rows) >= 512:
                            n_rows += db.upsert_records(rows)
                            rows = []
                    except (ValueError, TypeError, AttributeError, OverflowError):
                        skipped += 1
            n_rows += db.upsert_records(rows)
            if retired:
                from .db import _lock
                with _lock, db.conn:
                    db.conn.executemany('DELETE FROM records WHERE uid=?', retired)
            if res:
                skipped += res['skipped']
                res['commit']({'session_id': session, 'occurrences': occurrences})
            else:
                db.set_file(path, st.st_mtime, st.st_size, st.st_size, {})
        except (OSError, ValueError, TypeError, AttributeError, OverflowError):
            skipped += 1
    if migrate:
        db.set_kv('gemini_message_uids', 1)
    report["gemini"] = {"files": n_files, "new_records": n_rows, "skipped": skipped,
                        "skipped_large": skipped_large, "dirs": [str(root), *paths.load_config().get('extra_paths', {}).get('gemini', [])]}


def scan_all(db):
    report = {}
    for name, fn in (("claude", scan_claude), ("codex", scan_codex), ("gemini", scan_gemini)):
        try:
            fn(db, report)
        except Exception as e:  # one broken source must not stop the others
            report[name] = {"error": f"{type(e).__name__}: {e}"}
    return report
