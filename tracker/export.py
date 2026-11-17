"""Write the aggregated datasets the dashboard artifact reads (JSON arrays of rows).

    python app.py --export <folder>

Only counts, dates, model and project names leave the database; no prompts or code.
Thread titles come from Codex's own state database when it can be read.
"""
import json
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from . import paths, sources
from .limits import all_limits
from .pricing import PEAK_SQL, cost, price_for

TOK = ("input", "output", "cache_write", "cache_read")


def _source_name(key):
    return sources.names().get(key) or sources.display_name(key)


def _cost(r):
    return cost(r["model"], peak=r.get("peak"), **{k: r[k] or 0 for k in TOK})


def _codex_titles():
    out = {}
    for name in ("state_5.sqlite", "state_4.sqlite", "state.sqlite"):
        f = paths.codex_root() / name
        if not f.exists():
            continue
        try:
            con = sqlite3.connect(f"file:{f}?mode=ro", uri=True)
            for tid, title in con.execute("SELECT id, title FROM threads"):
                if title:
                    t = re.sub(r"\[([^\]]*)\]\([^)]*\)?", r"\1", title)  # [text](link) -> text
                    t = re.sub(r"\s+", " ", t).strip()
                    out[tid] = t[:70] + ("…" if len(t) > 70 else "")
            con.close()
        except sqlite3.Error:
            pass
        break
    return out


def build(db, limits=None):
    """All datasets as {name: rows}; used by --export and by the local dashboard."""
    q = db.query
    now = time.time()
    lday = "date(ts,'unixepoch','localtime')"

    daily = []
    for r in q(f"SELECT {lday} AS date, source, model, {PEAK_SQL} AS peak, SUM(input) input, SUM(output) output, "
               "SUM(cache_write) cache_write, SUM(cache_read) cache_read, SUM(reasoning) reasoning, "
               "COUNT(*) calls FROM records WHERE ts<=? GROUP BY date, source, model, peak ORDER BY date", (now,)):
        r["tool"] = _source_name(r.pop("source"))
        r["tokens"] = sum(r[k] or 0 for k in TOK)
        r["cost_usd"] = _cost(r)
        daily.append(r)

    proj = {}
    for r in q(f"SELECT COALESCE(project,'(알 수 없음)') project, source, model, {PEAK_SQL} AS peak, "
               "MIN(ts) first, MAX(ts) last, SUM(input) input, SUM(output) output, "
               "SUM(cache_write) cache_write, SUM(cache_read) cache_read, "
               "COUNT(*) calls FROM records WHERE ts<=? GROUP BY project, source, model, peak", (now,)):
        p = proj.setdefault(r["project"], {"project": r["project"], "tokens": 0, "cost_usd": 0.0, "calls": 0,
                                           "first": r["first"], "last": r["last"], "tools": set()})
        p["tokens"] += sum(r[k] or 0 for k in TOK)
        p["cost_usd"] += _cost(r) or 0
        p["calls"] += r["calls"]
        p["first"], p["last"] = min(p["first"], r["first"]), max(p["last"], r["last"])
        p["tools"].add(_source_name(r["source"]))
    projects = sorted(proj.values(), key=lambda p: -p["tokens"])
    for p in projects:
        p["cost_usd"] = round(p["cost_usd"], 2)
        p["tools"] = ", ".join(sorted(p["tools"]))
        p["first"] = datetime.fromtimestamp(p["first"]).strftime("%Y-%m-%d")
        p["last"] = datetime.fromtimestamp(p["last"]).strftime("%Y-%m-%d %H:%M")

    hours = q("SELECT CAST(strftime('%w',ts,'unixepoch','localtime') AS INT) weekday, "
              "CAST(strftime('%H',ts,'unixepoch','localtime') AS INT) hour, "
              "SUM(input+output+cache_write+cache_read) tokens, COUNT(*) calls FROM records "
              "WHERE ts<=? GROUP BY weekday, hour", (now,))
    for h in hours:
        h["weekday"] = (h["weekday"] + 6) % 7  # 0 = Monday

    titles = _codex_titles()
    sess = {}
    for r in q(f"SELECT session, source, COALESCE(project,'') project, model, {PEAK_SQL} AS peak, "
               "MIN(ts) start, MAX(ts) end, SUM(input) input, SUM(output) output, "
               "SUM(cache_write) cache_write, SUM(cache_read) cache_read, "
               "COUNT(*) calls FROM records WHERE ts<=? GROUP BY session, source, model, peak", (now,)):
        s = sess.setdefault((r["session"], r["source"]), {"session": r["session"], "tool": _source_name(r["source"]),
                                           "project": r["project"], "title": titles.get(r["session"], ""),
                                           "start": r["start"], "end": r["end"], "tokens": 0, "cost_usd": 0.0,
                                           "calls": 0, "models": set()})
        s["tokens"] += sum(r[k] or 0 for k in TOK)
        s["cost_usd"] += _cost(r) or 0
        s["calls"] += r["calls"]
        s["models"].add(r["model"])
        s["start"], s["end"] = min(s["start"], r["start"]), max(s["end"], r["end"])
    sessions = sorted(sess.values(), key=lambda s: -s["tokens"])[:60]
    for s in sessions:
        s["cost_usd"] = round(s["cost_usd"], 2)
        s["models"] = ", ".join(sorted(s["models"]))
        s["minutes"] = round((s["end"] - s["start"]) / 60)
        s["start"] = datetime.fromtimestamp(s["start"]).strftime("%Y-%m-%d %H:%M")
        s.pop("end")

    limit_rows = []
    lim = all_limits(db) if limits is None else limits
    for src, l in lim.items():
        accts = l.get("accounts") or ([{"plan": l.get("plan"), "windows": l.get("windows", []),
                                        "fetched_at": l.get("fetched_at")}] if l.get("windows") else [])
        for a in accts:
            for w in a["windows"]:
                limit_rows.append({
                    "tool": _source_name(src), "account": a.get("plan") or "이전 기록",
                    "window": w["label"], "used_pct": round(w["used_percent"], 1),
                    "left_pct": round(100 - w["used_percent"], 1),
                    "resets_at": datetime.fromtimestamp(w["resets_at"]).strftime("%Y-%m-%d %H:%M") if w.get("resets_at") else "",
                    "observed_at": datetime.fromtimestamp(a["fetched_at"]).strftime("%Y-%m-%d %H:%M") if a.get("fetched_at") else "",
                })

    # finer grain for the local dashboard's model / project filters
    detail = []
    for r in q(f"SELECT {lday} AS date, source, model, COALESCE(project,'(알 수 없음)') project, "
               f"{PEAK_SQL} AS peak, SUM(input) input, SUM(output) output, "
               "SUM(cache_write) cache_write, SUM(cache_read) cache_read, "
               "COUNT(*) calls FROM records WHERE ts<=? GROUP BY date, source, model, project, peak", (now,)):
        r["tool"] = _source_name(r.pop("source"))
        r["tokens"] = sum(r[k] or 0 for k in TOK)
        r["cost_usd"] = _cost(r)
        detail.append(r)
    hours_detail = q("SELECT CAST(strftime('%w',ts,'unixepoch','localtime') AS INT) weekday, "
                     "CAST(strftime('%H',ts,'unixepoch','localtime') AS INT) hour, source, model, "
                     "COALESCE(project,'(알 수 없음)') project, {lday} AS date, "
                     "SUM(input+output+cache_write+cache_read) tokens, COUNT(*) calls FROM records "
                     "WHERE ts<=? GROUP BY weekday, hour, source, model, project, date".replace("{lday}", lday), (now,))
    for h in hours_detail:
        h["weekday"] = (h["weekday"] + 6) % 7
        h["tool"] = _source_name(h.pop("source"))
    # per-model list prices, so the dashboard can work out what cache reads saved
    prices = {m: p for m in {r["model"] for r in detail} if (p := price_for(m))}
    for srow in sessions:
        srow["projects"] = srow["project"]
    unpriced = []
    for r in q("SELECT model, source, SUM(input+output+cache_write+cache_read) tokens FROM records "
               "WHERE ts>=? AND ts<=? GROUP BY model, source HAVING tokens>0 ORDER BY tokens DESC",
               (now - 90 * 86400, now)):
        if price_for(r["model"]) is None:
            unpriced.append({"model": r["model"], "tool": _source_name(r["source"]), "tokens": r["tokens"]})
    return {"daily": daily, "projects": projects, "hours": hours, "sessions": sessions, "limits": limit_rows,
            "daily_detail": detail, "hours_detail": hours_detail, "prices": prices, "limits_raw": _limits_raw(lim),
            "unpriced": unpriced}


def _limits_raw(lim):
    """Limits with epoch times, grouped per account, for the live dashboard."""
    out = []
    for src, l in lim.items():
        accts = l.get("accounts") or ([{"plan": l.get("plan"), "windows": l.get("windows", []),
                                        "fetched_at": l.get("fetched_at")}] if l.get("windows") else [])
        out.append({"tool": _source_name(src), "source": src, "status": l.get("status"), "message": l.get("message", ""),
                    "plan": l.get("plan"), "accounts": accts})
    return out


def export(db, folder):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    files = {f"{k}.json": v for k, v in build(db).items()
             if k in ("daily", "projects", "hours", "sessions", "limits")}
    for name, rows in files.items():
        (folder / name).write_text(json.dumps(rows, ensure_ascii=False, default=list), encoding="utf-8")
    return {n: len(r) for n, r in files.items()}
