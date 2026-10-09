"""Aggregations for the dashboard and widget. Dates are in the machine's local time."""
import time
from collections import defaultdict
from datetime import date, datetime, timedelta

from .pricing import cost

SOURCES = ("claude", "codex", "gemini")
TOK = ("input", "output", "cache_write", "cache_read")


def _local_midnight(days_ago=0):
    d = datetime.combine(date.today() - timedelta(days=days_ago), datetime.min.time())
    return int(d.timestamp())


def _sum_sql():
    return ", ".join(f"SUM({k}) AS {k}" for k in TOK) + ", SUM(reasoning) AS reasoning, COUNT(*) AS calls"


def _total(r):
    return sum(int(r.get(k) or 0) for k in TOK)


def _cost(r):
    c = cost(r.get("model"), **{k: int(r.get(k) or 0) for k in TOK})
    return c or 0.0


def _priced(r):
    return cost(r.get("model"), input=1) is not None


def today(db):
    """Small summary for the widget."""
    since = _local_midnight(0)
    rows = db.query(f"SELECT source, model, {_sum_sql()} FROM records WHERE ts>=? "
                    "GROUP BY source, model", (since,))
    out = {s: {"tokens": 0, "cost": 0.0} for s in SOURCES}
    for r in rows:
        o = out.setdefault(r["source"], {"tokens": 0, "cost": 0.0})
        o["tokens"] += _total(r)
        o["cost"] += _cost(r)
    out["all"] = {"tokens": sum(v["tokens"] for v in out.values()),
                  "cost": sum(v["cost"] for v in out.values())}
    return out


def dashboard(db, days=30, sources=None):
    sources = [s for s in (sources or SOURCES) if s in SOURCES] or list(SOURCES)
    ph = ",".join("?" * len(sources))
    since = _local_midnight(days - 1)
    base = f"FROM records WHERE ts>=? AND source IN ({ph})"
    args = (since, *sources)
    lday = "date(ts,'unixepoch','localtime')"

    # ---- daily x source x model (cost needs the model)
    daily = defaultdict(lambda: {s: {"tokens": 0, "cost": 0.0} for s in SOURCES})
    totals = {"tokens": 0, "cost": 0.0, "input": 0, "output": 0, "cache_write": 0,
              "cache_read": 0, "reasoning": 0, "calls": 0, "unpriced_tokens": 0}
    by_source = {s: {"tokens": 0, "cost": 0.0, "calls": 0} for s in SOURCES}
    for r in db.query(f"SELECT {lday} AS d, source, model, {_sum_sql()} {base} "
                      "GROUP BY d, source, model", args):
        t, c = _total(r), _cost(r)
        daily[r["d"]][r["source"]]["tokens"] += t
        daily[r["d"]][r["source"]]["cost"] += c
        by_source[r["source"]]["tokens"] += t
        by_source[r["source"]]["cost"] += c
        by_source[r["source"]]["calls"] += r["calls"]
        totals["tokens"] += t
        totals["cost"] += c
        for k in (*TOK, "reasoning", "calls"):
            totals[k] += int(r[k] or 0)
        if not _priced(r):
            totals["unpriced_tokens"] += t
    day_list = [(date.today() - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]
    daily_out = [{"date": d, **{s: daily[d][s] for s in SOURCES}} for d in day_list]

    # ---- models
    models = []
    for r in db.query(f"SELECT source, model, {_sum_sql()} {base} GROUP BY source, model", args):
        models.append({"source": r["source"], "model": r["model"], "tokens": _total(r),
                       "cost": _cost(r) if _priced(r) else None, "calls": r["calls"],
                       "cache_read": r["cache_read"], "output": r["output"]})
    models.sort(key=lambda m: -m["tokens"])

    # ---- weekday x hour (0=Mon)
    hours = [[0] * 24 for _ in range(7)]
    for r in db.query(f"SELECT CAST(strftime('%w',ts,'unixepoch','localtime') AS INT) AS w, "
                      f"CAST(strftime('%H',ts,'unixepoch','localtime') AS INT) AS h, "
                      f"{_sum_sql()} {base} GROUP BY w, h", args):
        hours[(r["w"] + 6) % 7][r["h"]] += _total(r)

    # ---- projects
    proj = defaultdict(lambda: {"tokens": 0, "cost": 0.0, "sources": set(), "last": 0})
    for r in db.query(f"SELECT COALESCE(project,'(알 수 없음)') AS p, source, model, MAX(ts) AS last, "
                      f"{_sum_sql()} {base} GROUP BY p, source, model", args):
        p = proj[r["p"]]
        p["tokens"] += _total(r)
        p["cost"] += _cost(r)
        p["sources"].add(r["source"])
        p["last"] = max(p["last"], r["last"])
    projects = sorted(({"project": k, **v, "sources": sorted(v["sources"])} for k, v in proj.items()),
                      key=lambda x: -x["tokens"])[:12]

    # ---- recent sessions
    sess = defaultdict(lambda: {"tokens": 0, "cost": 0.0, "start": 1e18, "end": 0,
                                "models": set(), "calls": 0})
    for r in db.query(f"SELECT session, source, COALESCE(project,'') AS project, model, "
                      f"MIN(ts) AS start, MAX(ts) AS end, {_sum_sql()} {base} "
                      "GROUP BY session, source, model", args):
        s = sess[(r["session"], r["source"], r["project"])]
        s["tokens"] += _total(r)
        s["cost"] += _cost(r)
        s["start"] = min(s["start"], r["start"])
        s["end"] = max(s["end"], r["end"])
        s["models"].add(r["model"])
        s["calls"] += r["calls"]
    sessions = sorted(({"session": k[0], "source": k[1], "project": k[2], **v,
                        "models": sorted(v["models"])} for k, v in sess.items()),
                      key=lambda x: -x["end"])[:25]

    # ---- yearly heatmap (always 365 days, selected sources)
    year = {r["d"]: _total(r) for r in db.query(
        f"SELECT {lday} AS d, {_sum_sql()} FROM records WHERE ts>=? AND source IN ({ph}) GROUP BY d",
        (_local_midnight(370), *sources))}

    denom = totals["input"] + totals["cache_read"] + totals["cache_write"]
    active_days = sum(1 for d in daily_out if any(d[s]["tokens"] for s in SOURCES))
    return {
        "generated_at": time.time(),
        "days": days,
        "sources": sources,
        "totals": {**totals, "cache_hit": (totals["cache_read"] / denom) if denom else 0,
                   "active_days": active_days},
        "today": today(db),
        "by_source": by_source,
        "daily": daily_out,
        "models": models,
        "hours": hours,
        "projects": projects,
        "sessions": sessions,
        "year": year,
        "coverage": coverage(db),
    }


def coverage(db):
    out = {}
    for r in db.query("SELECT source, COUNT(*) AS n, MIN(ts) AS first, MAX(ts) AS last "
                      "FROM records GROUP BY source"):
        out[r["source"]] = r
    return out
