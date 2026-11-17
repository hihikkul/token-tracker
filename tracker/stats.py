"""Aggregations for the dashboard and widget. Dates are in the machine's local time."""
import time
from collections import defaultdict
from datetime import datetime, timedelta

from . import sources as source_registry
from .pricing import PEAK_SQL, cost

TOK = ("input", "output", "cache_write", "cache_read")


def _local_midnight(days_ago=0, now=None):
    today = datetime.fromtimestamp(time.time() if now is None else now).date()
    d = datetime.combine(today - timedelta(days=days_ago), datetime.min.time())
    return int(d.timestamp())


def _sources(selected=None):
    names = source_registry.names()
    names.setdefault(source_registry.OTHER, source_registry.display_name(source_registry.OTHER))
    return list(names) if selected is None else list(dict.fromkeys(s for s in selected if s in names))


def _sum_sql():
    return ", ".join(f"SUM({k}) AS {k}" for k in TOK) + ", SUM(reasoning) AS reasoning, COUNT(*) AS calls"


def _total(r):
    return sum(int(r.get(k) or 0) for k in TOK)


def _cost(r):
    c = cost(r.get("model"), peak=r.get("peak"), **{k: int(r.get(k) or 0) for k in TOK})
    return c or 0.0


def _priced(r):
    return cost(r.get("model"), input=1, peak=r.get("peak")) is not None


def today(db, sources=None, now=None):
    """Small summary for the widget, or the dashboard's selected products."""
    now = time.time() if now is None else now
    selected = _sources(sources)
    ph = ",".join("?" * len(selected)) or "NULL"
    since = _local_midnight(0, now)
    rows = db.query(f"SELECT source, model, {PEAK_SQL} AS peak, {_sum_sql()} "
                    f"FROM records WHERE ts>=? AND ts<=? AND source IN ({ph}) "
                    "GROUP BY source, model, peak", (since, now, *selected))
    out = {s: {"tokens": 0, "cost": 0.0} for s in selected}
    for r in rows:
        o = out.setdefault(r["source"], {"tokens": 0, "cost": 0.0})
        o["tokens"] += _total(r)
        o["cost"] += _cost(r)
    out["all"] = {"tokens": sum(v["tokens"] for v in out.values()),
                  "cost": sum(v["cost"] for v in out.values())}
    return out


def dashboard(db, days=30, sources=None):
    now = time.time()
    sources = _sources(sources)
    ph = ",".join("?" * len(sources)) or "NULL"
    since = _local_midnight(days - 1, now)
    base = f"FROM records WHERE ts>=? AND ts<=? AND source IN ({ph})"
    args = (since, now, *sources)
    lday = "date(ts,'unixepoch','localtime')"

    # ---- daily x source x model (cost needs the model)
    daily = defaultdict(lambda: {s: {"tokens": 0, "cost": 0.0} for s in sources})
    totals = {"tokens": 0, "cost": 0.0, "input": 0, "output": 0, "cache_write": 0,
              "cache_read": 0, "reasoning": 0, "calls": 0, "unpriced_tokens": 0}
    by_source = {s: {"tokens": 0, "cost": 0.0, "calls": 0} for s in sources}
    for r in db.query(f"SELECT {lday} AS d, source, model, {PEAK_SQL} AS peak, {_sum_sql()} {base} "
                      "GROUP BY d, source, model, peak", args):
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
    local_today = datetime.fromtimestamp(now).date()
    day_list = [(local_today - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]
    daily_out = [{"date": d, **{s: daily[d][s] for s in sources}} for d in day_list]

    # ---- models
    by_model = {}
    for r in db.query(f"SELECT source, model, {PEAK_SQL} AS peak, {_sum_sql()} {base} "
                      "GROUP BY source, model, peak", args):
        m = by_model.setdefault((r["source"], r["model"]), {
            "source": r["source"], "model": r["model"], "tokens": 0,
            "cost": 0.0 if _priced(r) else None, "calls": 0, "cache_read": 0, "output": 0})
        m["tokens"] += _total(r)
        if m["cost"] is not None:
            m["cost"] += _cost(r)
        for k in ("calls", "cache_read", "output"):
            m[k] += int(r[k] or 0)
    models = sorted(by_model.values(), key=lambda m: -m["tokens"])

    # ---- weekday x hour (0=Mon)
    hours = [[0] * 24 for _ in range(7)]
    for r in db.query(f"SELECT CAST(strftime('%w',ts,'unixepoch','localtime') AS INT) AS w, "
                      f"CAST(strftime('%H',ts,'unixepoch','localtime') AS INT) AS h, "
                      f"{_sum_sql()} {base} GROUP BY w, h", args):
        hours[(r["w"] + 6) % 7][r["h"]] += _total(r)

    # ---- projects
    proj = defaultdict(lambda: {"tokens": 0, "cost": 0.0, "sources": set(), "last": 0})
    for r in db.query(f"SELECT COALESCE(project,'(알 수 없음)') AS p, source, model, MAX(ts) AS last, "
                      f"{PEAK_SQL} AS peak, {_sum_sql()} {base} GROUP BY p, source, model, peak", args):
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
                      f"MIN(ts) AS start, MAX(ts) AS end, {PEAK_SQL} AS peak, {_sum_sql()} {base} "
                      "GROUP BY session, source, project, model, peak", args):
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
        f"SELECT {lday} AS d, {_sum_sql()} FROM records WHERE ts>=? AND ts<=? "
        f"AND source IN ({ph}) GROUP BY d", (_local_midnight(370, now), now, *sources))}

    denom = totals["input"] + totals["cache_read"] + totals["cache_write"]
    active_days = sum(1 for d in daily_out if any(d[s]["tokens"] for s in sources))
    return {
        "generated_at": now,
        "days": days,
        "sources": sources,
        "totals": {**totals, "cache_hit": (totals["cache_read"] / denom) if denom else 0,
                   "active_days": active_days},
        "today": today(db, sources=sources, now=now),
        "by_source": by_source,
        "daily": daily_out,
        "models": models,
        "hours": hours,
        "projects": projects,
        "sessions": sessions,
        "year": year,
        "coverage": coverage(db, now=now, sources=sources),
    }


def coverage(db, now=None, sources=None):
    now = time.time() if now is None else now
    selected = _sources(sources)
    ph = ",".join("?" * len(selected)) or "NULL"
    out = {}
    for r in db.query("SELECT source, COUNT(*) AS n, MIN(ts) AS first, MAX(ts) AS last "
                      f"FROM records WHERE ts<=? AND source IN ({ph}) GROUP BY source", (now, *selected)):
        out[r["source"]] = r
    return out
