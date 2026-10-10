"""쌀먹 비율 (API-equivalent spend ÷ subscription) and the group ranking client.

Periods follow the local calendar: today, this week (from Monday), this month.
Subscription comparisons use a full paid month, or its fixed 1/30 daily and
7/30 weekly allocation. Month-to-date usage is never compared with a partial bill.

Subscription price per tool: `subscription_usd_month` in config.json when > 0,
otherwise the price of the plan the limits lookup reports (Claude Pro, Codex Plus…).
Gemini has no plan lookup, so it counts only when set in config.

The ranking server (server/worker.js) receives nickname, plan names and, per period,
the window plus two dollar amounts. Nothing else leaves the PC.
"""
import json
import secrets
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse
from datetime import date, datetime, timedelta

from . import paths
from .pricing import cost
from .sources import enabled

# Shared ranking service. config.json `ranking.server` overrides this address.
DEFAULT_SERVER = "https://token-tracker-rank.hihikkul-token-tracker.workers.dev"

SOURCES = ("claude", "codex", "gemini")
NAMES = {"claude": "Claude", "codex": "Codex", "gemini": "Gemini"}
# monthly USD list prices of the plans the limits lookup can report
PLAN_PRICES = {
    "claude": {"free": 0, "pro": 20, "max 5x": 100, "max 20x": 200, "max": 100, "team": 30},
    "codex": {"free": 0, "go": 8, "plus": 20, "pro": 200, "team": 30, "business": 30},
}
PERIODS = ("day", "week", "month")
SYNC_EVERY = 15 * 60


def windows(today=None):
    """{period: (window id, start date, days elapsed)} for the local calendar."""
    d = today or date.today()
    monday = d - timedelta(days=d.weekday())
    iso = d.isocalendar()
    return {
        "day": (d.isoformat(), d, 1),
        "week": (f"{iso[0]}-W{iso[1]:02d}", monday, d.weekday() + 1),
        "month": (d.strftime("%Y-%m"), d.replace(day=1), d.day),
    }


def _plan(limits, src):
    lim = (limits or {}).get(src) or {}
    if src == 'gemini' and not lim.get('plan'):
        lim = (limits or {}).get('antigravity') or lim
    if lim.get("accounts"):
        return lim["accounts"][0].get("plan")
    return lim.get("plan")


def subscriptions(limits, cfg=None):
    """{src: {"usd": float|None, "plan": str|None, "from": "config"|"plan"|None}}"""
    cfg = cfg or paths.load_config()
    manual = cfg.get("subscription_usd_month") or {}
    selected = {'gemini' if src == 'antigravity' else src for src in enabled(cfg)}
    out = {}
    for src in SOURCES:
        if src not in selected:
            out[src] = {'usd': None, 'plan': None, 'from': None}
            continue
        plan = _plan(limits, src)
        m = manual.get(src)
        if isinstance(m, (int, float)) and m > 0:
            out[src] = {"usd": float(m), "plan": plan, "from": "config"}
            continue
        key = (plan or "").lower().split(" ·")[0].strip()
        price = PLAN_PRICES.get(src, {}).get(key)
        out[src] = {"usd": float(price) if price is not None else None, "plan": plan,
                    "from": "plan" if price is not None else None}
    return out


def _spend(db, since_ts, until_ts=None, cfg=None):
    """{src: API-equivalent USD} for records at or after since_ts."""
    out = {s: 0.0 for s in SOURCES}
    selected = set(enabled(cfg))
    rows = db.query("SELECT source, model, SUM(input) input, SUM(output) output, SUM(cache_write) cache_write, "
                    "SUM(cache_read) cache_read FROM records WHERE ts >= ? AND ts <= ? GROUP BY source, model",
                    (since_ts, until_ts if until_ts is not None else time.time()))
    for r in rows:
        c = cost(r["model"], input=r["input"] or 0, output=r["output"] or 0,
                 cache_write=r["cache_write"] or 0, cache_read=r["cache_read"] or 0)
        source = 'gemini' if r['source'] == 'antigravity' else r['source']
        if c and source in out and r['source'] in selected:
            out[source] += c
    return out


def report(db, limits, cfg=None):
    """쌀먹 numbers per period, in total and per tool."""
    subs = subscriptions(limits, cfg)
    res = {"subscriptions": subs, "periods": {}}
    for period, (win, start, days) in windows().items():
        spend = _spend(db, datetime.combine(start, datetime.min.time()).timestamp(), cfg=cfg)
        tools, t_api, t_sub = {}, 0.0, 0.0
        for src in SOURCES:
            usd = subs[src]["usd"]
            share = usd * {'day': 1 / 30, 'week': 7 / 30, 'month': 1}[period] if usd else 0.0
            api = spend[src]
            if not usd and not api:
                continue
            tools[src] = {"api": round(api, 4), "sub": round(share, 4), "ratio": (api / share) if share else None,
                          "plan": subs[src]["plan"], "from": subs[src]["from"], "monthly": usd}
            if share:  # a tool without a known price can't be part of the ratio
                t_api += api
                t_sub += share
        excluded = [src for src, tool in tools.items() if tool['api'] and not tool['sub']]
        res["periods"][period] = {"window": win, "days": days, "api": round(t_api, 4), "sub": round(t_sub, 4),
                                  "ratio": (t_api / t_sub) if t_sub else None, "tools": tools,
                                  'excluded': excluded, 'basis': 'full_period', 'calculation_version': 2}
    return res


# ------------------------------------------------------------------ server client
class RankingError(Exception):
    pass


def server_url(cfg=None):
    cfg = cfg or paths.load_config()
    settings = cfg.get('ranking') or {}
    url = (settings.get('server') or DEFAULT_SERVER).rstrip('/')
    host = urlparse(url).hostname
    if host in ('localhost', '127.0.0.1', '::1') and not settings.get('allow_local_server'):
        return DEFAULT_SERVER
    return url


def _call(method, path, body=None, token=None, cfg=None):
    base = server_url(cfg)
    if not base:
        raise RankingError("No ranking server configured")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json", "User-Agent": "token-tracker",
                                          **({"Authorization": f"Bearer {token}"} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            return json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode("utf-8")).get("error")
        except (ValueError, OSError):
            msg = None
        raise RankingError({"group not found": "Invite code not found", "group is full": "Group is full (50 members maximum)",
                            "bad token": "Membership expired. Join the group again."}.get(msg, msg or f"Server error {e.code}"))
    except (urllib.error.URLError, TimeoutError, OSError):
        raise RankingError("Could not connect to the public ranking server. Update Token Tracker and check your connection.")


def state(cfg=None):
    cfg = cfg or paths.load_config()
    r = cfg.get("ranking") or {}
    return {"server": bool(server_url(cfg)), 'server_url': server_url(cfg),
            'invite_url': server_url(cfg) + '/?invite=' + str(r.get('code') or ''),
            "joined": bool(r.get("code") and r.get("token")),
            "code": r.get("code"), "group_name": r.get("group_name"), "nick": r.get("nick") or ""}


def _save(**kv):
    cfg = paths.load_config()
    cfg.setdefault("ranking", {}).update(kv)
    paths.save_config(cfg)


def create(group_name, nick):
    res = _call("POST", "/v1/groups", {"group_name": group_name, "nick": nick})
    _save(code=res["code"], group_name=res.get("name"), member_id=res["member_id"], token=res["token"], nick=nick)
    return res


def join(code, nick):
    code = code.strip().upper()
    res = _call("POST", f"/v1/groups/{code}/join", {"nick": nick})
    _save(code=res["code"], group_name=res.get("name"), member_id=res["member_id"], token=res["token"], nick=nick)
    return res


def leave():
    cfg = paths.load_config()
    r = cfg.get("ranking") or {}
    if r.get("code") and r.get("token"):
        try:
            _call("DELETE", f"/v1/groups/{r['code']}/me", token=r["token"], cfg=cfg)
        except RankingError:
            pass  # leave locally even if the server is unreachable
    _save(code=None, group_name=None, member_id=None, token=None)


def set_nick(nick):
    _save(nick=nick)


_lock = threading.Lock()
_last_sync = {"t": 0.0}


def sync(db, limits, force=False):
    """Send this PC's numbers to the group. Quiet no-op when not joined or synced recently."""
    cfg = paths.load_config()
    r = cfg.get("ranking") or {}
    if not (r.get("code") and r.get("token") and server_url(cfg)):
        return False
    with _lock:
        if not force and time.time() - _last_sync["t"] < SYNC_EVERY:
            return False
        rep = report(db, limits, cfg)
        plans = " · ".join(f"{NAMES[s]} {t['plan']}" if t.get("plan") else NAMES[s]
                           for s, t in rep["periods"]["month"]["tools"].items() if t["sub"])
        body = {"nick": r.get("nick") or "", "plans": plans, 'calculation_version': 2,
                "periods": {p: {"window": v["window"], "api": round(v["api"], 4), "sub": round(v["sub"], 4)}
                            for p, v in rep["periods"].items()}}
        _call("PUT", f"/v1/groups/{r['code']}/score", body, token=r["token"], cfg=cfg)
        _last_sync["t"] = time.time()
        return True


def ranking(db, limits, period):
    cfg = paths.load_config()
    r = cfg.get("ranking") or {}
    if not (r.get("code") and r.get("token")):
        raise RankingError("You have not joined a group yet")
    try:
        sync(db, limits, force=time.time() - _last_sync["t"] > 120)
    except RankingError:
        pass  # show the last ranking even if our own update failed
    period = period if period in PERIODS else "week"
    win = windows()[period][0]
    res = _call("GET", f"/v1/groups/{r['code']}/ranking?period={period}&window={win}", token=r["token"], cfg=cfg)
    res["nick"] = r.get("nick")
    return res


def random_nick():
    return "User-" + secrets.token_hex(2)
