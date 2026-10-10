"""Read the logs each tool already writes and turn them into usage records.

Claude Code : ~/.claude/projects/**/*.jsonl         (assistant lines with message.usage)
Codex       : ~/.codex/sessions/**/rollout-*.jsonl  (event_msg token_count, cumulative)
Gemini CLI  : ~/.gemini/tmp/*/chats/*.json          (messages[].tokens)
Antigravity : ~/.gemini/antigravity*/brain/*/...    (best effort; current builds log no token counts)

JSONL files are append-only, so they are read incrementally from the last offset.
"""
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from . import paths


def parse_ts(v):
    """ISO string / epoch seconds / epoch ms -> unix seconds (int) or None."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v / 1000) if v > 1e11 else int(v)
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


def read_new_lines(db, path):
    """Yield (lineno, obj) for complete JSON lines added since the last scan.
    Returns via generator; the caller must call commit(state) when done."""
    st = path.stat()
    rec = db.get_file(path)
    offset, state = 0, {}
    if rec:
        if rec["size"] == st.st_size and rec["mtime"] == st.st_mtime:
            return None  # unchanged
        if st.st_size >= rec["offset"]:
            offset, state = rec["offset"], rec["state"]
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read()
    end = data.rfind(b"\n")
    if end < 0:
        return {"lines": [], "state": state, "commit": lambda s: None}
    chunk = data[: end + 1]
    new_offset = offset + end + 1
    lines = []
    base_line = state.get("_lines", 0)
    for i, raw in enumerate(chunk.split(b"\n")):
        raw = raw.strip()
        if not raw:
            continue
        try:
            lines.append((base_line + i, json.loads(raw)))
        except ValueError:
            continue
    nlines = base_line + chunk.count(b"\n")

    def commit(new_state):
        new_state = dict(new_state or {})
        new_state["_lines"] = nlines
        db.set_file(path, st.st_mtime, st.st_size, new_offset, new_state)

    return {"lines": lines, "state": state, "commit": commit}


# ======================================================================= Claude
def _claude_dir_project(path):
    """Fallback when a line has no cwd: 'C--Users-me-quant-vault' -> 'quant-vault'."""
    for parent in path.parents:
        if parent.parent.name == "projects":
            name = parent.name
            m = re.match(r"^[A-Za-z]--Users-[^-]+-(.+)$", name) or re.match(r"^-home-[^-]+-(.+)$", name)
            return m.group(1) if m else name
    return path.parent.name


def scan_claude(db, report):
    n_files = n_rows = 0
    for root in paths.claude_project_dirs():
        for path in root.rglob("*.jsonl"):
            n_files += 1
            try:
                res = read_new_lines(db, path)
            except OSError:
                continue
            if res is None:
                continue
            rows = []
            for lineno, o in res["lines"]:
                if not isinstance(o, dict) or o.get("type") != "assistant":
                    continue
                msg = o.get("message") or {}
                u = msg.get("usage")
                model = msg.get("model")
                if not u or not model or model == "<synthetic>":
                    continue
                ts = parse_ts(o.get("timestamp"))
                if ts is None:
                    continue
                mid, rid = msg.get("id"), o.get("requestId")
                uid = f"c:{mid}:{rid}" if mid else f"c:{path}:{lineno}"
                rows.append({
                    "uid": uid, "source": "claude", "ts": ts, "model": model,
                    "project": _project_name(o.get("cwd")) or _claude_dir_project(path),
                    "session": o.get("sessionId") or path.stem,
                    "input": u.get("input_tokens", 0),
                    "output": u.get("output_tokens", 0),
                    "cache_write": u.get("cache_creation_input_tokens", 0),
                    "cache_read": u.get("cache_read_input_tokens", 0),
                })
            n_rows += db.upsert_records(rows)
            res["commit"](res["state"])
    report["claude"] = {"files": n_files, "new_records": n_rows,
                        "dirs": [str(p) for p in paths.claude_project_dirs()]}


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
        resets_at = parse_ts(w.get("resets_at"))
        if resets_at is None and w.get("resets_in_seconds") is not None:
            resets_at = ts + int(w["resets_in_seconds"])
        out["windows"].append({
            "key": key,
            "used_percent": float(w["used_percent"]),
            "window_minutes": w.get("window_minutes"),
            "resets_at": resets_at,
        })
    if rl.get("plan_type"):
        out["plan"] = rl["plan_type"]
    if rl.get("rate_limit_reached_type"):
        out["reached"] = rl["rate_limit_reached_type"]
    return out if out["windows"] else None


def _limits_key(snap, acct):
    # A session keeps the account it was started with (creator_account_id), but you can switch
    # login mid-session, so the plan reported with the limits is the reliable identity.
    who = f"plan:{snap['plan']}" if snap.get("plan") else (acct or "default")
    return f"{who}:{snap['limit_id']}"


def _keep_limits(accounts, snap, acct, plans, learn):
    """Store the newest snapshot per quota. Returns True once this file taught a plan.

    Newer Codex builds often leave plan_type out. Such snapshots stay keyed by login and get
    a plan_guess for their label; the guess comes from the first labelled snapshot of a
    session, which belongs to the login that started it (a mid-session switch comes later)."""
    who = acct or "default"
    if snap.get("plan"):
        if learn:
            plans[who] = snap["plan"]
            learn = False
    elif plans.get(who):
        snap["plan_guess"] = plans[who]
    key = _limits_key(snap, acct)
    if snap["ts"] >= (accounts.get(key) or {}).get("ts", 0):
        snap["account"] = acct
        accounts[key] = snap
    return learn


LIMITS_VERSION = 4


def _rebuild_codex_limits(db):
    """One-off re-read of the last 8 days of Codex logs after the limits keying changed."""
    accounts, plans, cutoff = {}, {}, time.time() - 8 * 86400
    files = []
    for d in paths.codex_session_dirs():
        for path in d.rglob("*.jsonl"):
            try:
                if path.stat().st_mtime >= cutoff:
                    files.append(path)
            except OSError:
                pass
    for path in sorted(files, key=lambda x: x.name):
        if True:
            try:
                f = open(path, encoding="utf-8", errors="replace")
            except OSError:
                continue
            acct, learn = None, True
            with f:
                for line in f:
                    if '"session_meta"' not in line and '"rate_limits"' not in line:
                        continue
                    try:
                        o = json.loads(line)
                    except ValueError:
                        continue
                    p = o.get("payload") or {}
                    if o.get("type") == "session_meta":
                        acct = p.get("creator_account_id") or acct
                    elif o.get("type") == "event_msg" and p.get("type") == "token_count":
                        ts = parse_ts(o.get("timestamp"))
                        snap = _codex_limits_snapshot(p.get("rate_limits"), ts) if ts else None
                        if snap:
                            learn = _keep_limits(accounts, snap, acct, plans, learn)
    db.set_kv("codex_limits_by_account", accounts)
    db.set_kv("codex_plan_by_account", plans)
    db.set_kv("codex_limits_version", LIMITS_VERSION)


def scan_codex(db, report):
    """Codex writes a cumulative total_token_usage on every token_count event; the
    per-call usage is the difference to the previous event in the same file."""
    n_files = n_rows = 0
    if db.get_kv("codex_limits_version") != LIMITS_VERSION:
        _rebuild_codex_limits(db)
    accounts = db.get_kv("codex_limits_by_account") or {}  # plan -> latest snapshot
    plans = db.get_kv("codex_plan_by_account") or {}        # login -> last plan seen
    for d in paths.codex_session_dirs():
        for path in d.rglob("*.jsonl"):
            n_files += 1
            try:
                res = read_new_lines(db, path)
            except OSError:
                continue
            if res is None:
                continue
            state = dict(res["state"])
            sid = state.get("sid")
            if not sid:
                m = _UUID.search(path.name)
                sid = m.group(0) if m else path.stem
            model, cwd, acct = state.get("model"), state.get("cwd"), state.get("acct")
            learn = state.get("learn", True)
            prev = state.get("prev") or {}
            rows = []
            for _, o in res["lines"]:
                if not isinstance(o, dict):
                    continue
                typ, p = o.get("type"), o.get("payload") or {}
                if typ == "session_meta":
                    sid = p.get("id") or p.get("session_id") or sid
                    cwd = p.get("cwd") or cwd
                    model = p.get("model") or model
                    acct = p.get("creator_account_id") or acct
                    continue
                if typ == "turn_context":
                    model = p.get("model") or model
                    cwd = p.get("cwd") or cwd
                    continue
                if typ != "event_msg" or p.get("type") != "token_count":
                    continue
                ts = parse_ts(o.get("timestamp"))
                if ts is None:
                    continue
                snap = _codex_limits_snapshot(p.get("rate_limits"), ts)
                if snap:
                    learn = _keep_limits(accounts, snap, acct, plans, learn)
                info = p.get("info") or {}
                tot = info.get("total_token_usage")
                if not isinstance(tot, dict):
                    continue
                cur = {k: int(tot.get(k) or 0) for k in
                       ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
                        "output_tokens", "reasoning_output_tokens", "total_tokens")}
                if cur["total_tokens"] < prev.get("total_tokens", 0):
                    prev = {}  # counter restarted (new conversation in same file)
                delta = {k: cur[k] - prev.get(k, 0) for k in cur}
                prev = cur
                if delta["total_tokens"] <= 0:
                    continue  # repeated event, nothing new
                cached = max(0, delta["cached_input_tokens"])
                cwrite = max(0, delta["cache_write_input_tokens"])
                rows.append({
                    # session id + cumulative total is stable even if the file is
                    # later moved to archived_sessions, so it is never counted twice
                    "uid": f"x:{sid}:{cur['total_tokens']}", "source": "codex", "ts": ts,
                    "model": model or "codex", "project": _project_name(cwd),
                    "session": sid,
                    "input": max(0, delta["input_tokens"] - cached - cwrite),
                    "output": max(0, delta["output_tokens"]),
                    "cache_read": cached, "cache_write": cwrite,
                    "reasoning": max(0, delta["reasoning_output_tokens"]),
                })
            n_rows += db.upsert_records(rows)
            res["commit"]({"sid": sid, "model": model, "cwd": cwd, "acct": acct, "prev": prev, "learn": learn})
    if accounts:
        db.set_kv("codex_limits_by_account", accounts)
        db.set_kv("codex_plan_by_account", plans)
    report["codex"] = {"files": n_files, "new_records": n_rows, "accounts": len(accounts),
                       "dirs": [str(p) for p in paths.codex_session_dirs()]}


# ======================================================================= Gemini
def _gemini_usage(d):
    """Return normalized token dict from a Gemini-style object, or None."""
    t = d.get("tokens")
    if isinstance(t, dict) and ("input" in t or "output" in t):
        inp, cached = int(t.get("input") or 0), int(t.get("cached") or 0)
        thoughts = int(t.get("thoughts") or 0)
        return {"input": max(0, inp - cached) + int(t.get("tool") or 0),
                "output": int(t.get("output") or 0) + thoughts,
                "cache_read": cached, "reasoning": thoughts}
    u = d.get("usageMetadata") or d.get("usage_metadata")
    if isinstance(u, dict) and ("promptTokenCount" in u or "candidatesTokenCount" in u):
        inp = int(u.get("promptTokenCount") or 0)
        cached = int(u.get("cachedContentTokenCount") or 0)
        thoughts = int(u.get("thoughtsTokenCount") or 0)
        return {"input": max(0, inp - cached) + int(u.get("toolUsePromptTokenCount") or 0),
                "output": int(u.get("candidatesTokenCount") or 0) + thoughts,
                "cache_read": cached, "reasoning": thoughts}
    return None


def _walk(o, parent_ts=None, parent_model=None, depth=0):
    """Yield (dict, ts, model) for every dict carrying token usage."""
    if depth > 12:
        return
    if isinstance(o, dict):
        ts = parse_ts(o.get("timestamp") or o.get("createTime") or o.get("time")) or parent_ts
        model = o.get("model") or o.get("modelVersion") or parent_model
        if _gemini_usage(o):
            yield o, ts, model
        for v in o.values():
            if isinstance(v, (dict, list)):
                yield from _walk(v, ts, model, depth + 1)
    elif isinstance(o, list):
        for v in o:
            yield from _walk(v, parent_ts, parent_model, depth + 1)


def _gemini_files():
    root = paths.gemini_root()
    if not root.exists():
        return []
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


def scan_gemini(db, report):
    n_files = n_rows = 0
    root = paths.gemini_root()
    for path in _gemini_files():
        try:
            st = path.stat()
        except OSError:
            continue
        if st.st_size > 80 * 1024 * 1024:
            continue
        n_files += 1
        rec = db.get_file(path)
        if rec and rec["mtime"] == st.st_mtime and rec["size"] == st.st_size:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        objs = []
        if path.suffix == ".jsonl":
            for line in text.splitlines():
                try:
                    objs.append(json.loads(line))
                except ValueError:
                    pass
        else:
            try:
                objs.append(json.loads(text))
            except ValueError:
                pass
        rel = str(path.relative_to(root)) if root in path.parents else str(path)
        kind = "antigravity" if "antigravity" in rel.lower() else "gemini-cli"
        session = path.stem
        if objs and isinstance(objs[0], dict):
            session = objs[0].get("sessionId") or session
        fallback_ts = int(st.st_mtime)
        rows = []
        for idx, (d, ts, model) in enumerate(x for o in objs for x in _walk(o)):
            u = _gemini_usage(d)
            mid = d.get("id") or idx
            rows.append({
                "uid": f"g:{rel}:{mid}", "source": 'antigravity' if kind == 'antigravity' else 'gemini', "ts": ts or fallback_ts,
                "model": model or "gemini", "project": kind if kind == "antigravity"
                else (path.parent.parent.name[:8] if path.parent.name == "chats" else kind),
                "session": session, "cache_write": 0, **u,
            })
        n_rows += db.upsert_records(rows)
        db.set_file(path, st.st_mtime, st.st_size, st.st_size, {})
    report["gemini"] = {"files": n_files, "new_records": n_rows, "dirs": [str(root)]}


def scan_all(db):
    report = {}
    for name, fn in (("claude", scan_claude), ("codex", scan_codex), ("gemini", scan_gemini)):
        try:
            fn(db, report)
        except Exception as e:  # one broken source must not stop the others
            report[name] = {"error": f"{type(e).__name__}: {e}"}
    return report
