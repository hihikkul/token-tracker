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

from . import paths
from .limits import all_limits
from .pricing import cost, price_for

TOK = ("input", "output", "cache_write", "cache_read")
from .sources import NAMES as SRC_NAME


def _cost(r):
    c = cost(r["model"], **{k: r[k] or 0 for k in TOK})
    return round(c, 4) if c is not None else None


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
    lday = "date(ts,'unixepoch','localtime')"

    daily = []
    for r in q(f"SELECT {lday} AS date, source, model, SUM(input) input, SUM(output) output, "
               "SUM(cache_write) cache_write, SUM(cache_read) cache_read, SUM(reasoning) reasoning, "
               "COUNT(*) calls FROM records GROUP BY date, source, model ORDER BY date"):
        r["tool"] = SRC_NAME.get(r.pop("source"), "?")
        r["tokens"] = sum(r[k] or 0 for k in TOK)
        r["cost_usd"] = _cost(r)
        daily.append(r)

    proj = {}
    for r in q("SELECT COALESCE(project,'(알 수 없음)') project, source, model, MIN(ts) first, MAX(ts) last, "
               "SUM(input) input, SUM(output) output, SUM(cache_write) cache_write, SUM(cache_read) cache_read, "
               "COUNT(*) calls FROM records GROUP BY project, source, model"):
        p = proj.setdefault(r["project"], {"project": r["project"], "tokens": 0, "cost_usd": 0.0, "calls": 0,
                                           "first": r["first"], "last": r["last"], "tools": set()})
        p["tokens"] += sum(r[k] or 0 for k in TOK)
        p["cost_usd"] += _cost(r) or 0
        p["calls"] += r["calls"]
        p["first"], p["last"] = min(p["first"], r["first"]), max(p["last"], r["last"])
        p["tools"].add(SRC_NAME.get(r["source"]))
    projects = sorted(proj.values(), key=lambda p: -p["tokens"])
    for p in projects:
        p["cost_usd"] = round(p["cost_usd"], 2)
        p["tools"] = ", ".join(sorted(p["tools"]))
        p["first"] = datetime.fromtimestamp(p["first"]).strftime("%Y-%m-%d")
        p["last"] = datetime.fromtimestamp(p["last"]).strftime("%Y-%m-%d %H:%M")

    hours = q("SELECT CAST(strftime('%w',ts,'unixepoch','localtime') AS INT) weekday, "
              "CAST(strftime('%H',ts,'unixepoch','localtime') AS INT) hour, "
              "SUM(input+output+cache_write+cache_read) tokens, COUNT(*) calls FROM records GROUP BY weekday, hour")
    for h in hours:
        h["weekday"] = (h["weekday"] + 6) % 7  # 0 = Monday

    titles = _codex_titles()
    sess = {}
    for r in q("SELECT session, source, COALESCE(project,'') project, model, MIN(ts) start, MAX(ts) end, "
               "SUM(input) input, SUM(output) output, SUM(cache_write) cache_write, SUM(cache_read) cache_read, "
               "COUNT(*) calls FROM records GROUP BY session, source, model"):
        s = sess.setdefault(r["session"], {"session": r["session"], "tool": SRC_NAME.get(r["source"]),
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
                    "tool": SRC_NAME[src], "account": a.get("plan") or "이전 기록",
                    "window": w["label"], "used_pct": round(w["used_percent"], 1),
                    "left_pct": round(100 - w["used_percent"], 1),
                    "resets_at": datetime.fromtimestamp(w["resets_at"]).strftime("%Y-%m-%d %H:%M") if w.get("resets_at") else "",
                    "observed_at": datetime.fromtimestamp(a["fetched_at"]).strftime("%Y-%m-%d %H:%M") if a.get("fetched_at") else "",
                })

    # finer grain for the local dashboard's model / project filters
    detail = []
    for r in q(f"SELECT {lday} AS date, source, model, COALESCE(project,'(알 수 없음)') project, "
               "SUM(input) input, SUM(output) output, SUM(cache_write) cache_write, SUM(cache_read) cache_read, "
               "COUNT(*) calls FROM records GROUP BY date, source, model, project"):
        r["tool"] = SRC_NAME.get(r.pop("source"), "?")
        r["tokens"] = sum(r[k] or 0 for k in TOK)
        r["cost_usd"] = _cost(r)
        detail.append(r)
    hours_detail = q("SELECT CAST(strftime('%w',ts,'unixepoch','localtime') AS INT) weekday, "
                     "CAST(strftime('%H',ts,'unixepoch','localtime') AS INT) hour, source, model, "
                     "COALESCE(project,'(알 수 없음)') project, {lday} AS date, "
                     "SUM(input+output+cache_write+cache_read) tokens, COUNT(*) calls FROM records "
                     "GROUP BY weekday, hour, source, model, project, date".replace("{lday}", lday))
    for h in hours_detail:
        h["weekday"] = (h["weekday"] + 6) % 7
        h["tool"] = SRC_NAME.get(h.pop("source"), "?")
    # per-model list prices, so the dashboard can work out what cache reads saved
    prices = {m: p for m in {r["model"] for r in detail} if (p := price_for(m))}
    for srow in sessions:
        srow["projects"] = srow["project"]
    return {"daily": daily, "projects": projects, "hours": hours, "sessions": sessions, "limits": limit_rows,
            "daily_detail": detail, "hours_detail": hours_detail, "prices": prices, "limits_raw": _limits_raw(lim)}


def _limits_raw(lim):
    """Limits with epoch times, grouped per account, for the live dashboard."""
    out = []
    for src, l in lim.items():
        accts = l.get("accounts") or ([{"plan": l.get("plan"), "windows": l.get("windows", []),
                                        "fetched_at": l.get("fetched_at")}] if l.get("windows") else [])
        out.append({"tool": SRC_NAME[src], "source": src, "status": l.get("status"), "message": l.get("message", ""),
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
