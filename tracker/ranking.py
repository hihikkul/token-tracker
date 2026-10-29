"""쌀먹 비율 (API-equivalent spend ÷ subscription) and the group ranking client.

Periods follow the local calendar: today, this week (from Monday), this month.
Subscription comparisons use a full paid month, or its fixed 1/30 daily and
7/30 weekly allocation. Month-to-date usage is never compared with a partial bill.

Subscription price per tool: the selected subscription_plans entry, then a legacy
subscription_usd_month override, otherwise the detected plan's list price.
Free and unknown selected plans never fall back to a detected paid plan.

The ranking server (server/worker.js) receives nickname, plan names and, per period,
the window, three dollar amounts (API-equivalent spend counted in the ratio, the
subscription share, API-equivalent spend of every tool) and the token total. Nothing else
leaves the PC. Both groups and the opt-in public ranking rank by ratio.
"""
import json
import hashlib
import secrets
import re
import unicodedata
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse
from datetime import date, datetime, timedelta

from . import paths
from .pricing import cost
from .sources import enabled

# Shared ranking service. config.json `ranking.server` overrides this address.
DEFAULT_SERVER = "https://token-tracker-rank.hihikkul-token-tracker.workers.dev"

SOURCES = ("claude", "codex", "gemini")
NAMES = {"claude": "Claude", "codex": "Codex", "gemini": "Gemini"}
# USD monthly list prices; annual options use the monthly equivalent.
# Sources: claude.com/pricing, help.openai.com (Plus, Pro tiers, Business),
# blog.google/products/google-one/google-ai-ultra/ and Google AI plans.
PLAN_OPTIONS = {
    'claude': {
        'free': ('Free', 0), 'pro': ('Pro', 20),
        'pro_annual': ('Pro (annual)', 200 / 12),
        'max_5x': ('Max 5x', 100), 'max_20x': ('Max 20x', 200),
        'team': ('Team Standard', 25), 'team_premium': ('Team Premium', 125),
        'enterprise': ('Enterprise', None)},
    'codex': {
        'free': ('Free', 0), 'go': ('Go', None), 'plus': ('Plus', 20),
        'pro_100': ('Pro 100', 100), 'pro_200': ('Pro 200', 200), 'pro_500': ('Pro 500', 500),
        'business': ('Business Standard', 25), 'business_annual': ('Business Standard (annual)', 20),
        'business_premium': ('Business Premium', 125), 'business_premium_annual': ('Business Premium (annual)', 100),
        'business_lite': ('Business Lite', None), 'enterprise': ('Enterprise', None)},
    'gemini': {'free': ('Free', 0), 'plus': ('Google AI Plus', None),
               'pro': ('Google AI Pro', 19.99), 'pro_annual': ('Google AI Pro (annual)', 199.99 / 12),
               'ultra_5x': ('Google AI Ultra 5x', 99.99), 'ultra': ('Google AI Ultra 20x', 249.99),
               'other': ('Other plan', None)},
}
def _plan_key(value):
    """Match display names, internal IDs and account suffixes without guessing prices."""
    if not isinstance(value, str):
        return ''
    value = unicodedata.normalize('NFKC', value).casefold().split('·', 1)[0]
    value = value.replace('×', 'x')
    return re.sub(r'[^a-z0-9]+', '', value)


# One source of truth for selected AND detected prices, including unknown/free plans.
PLAN_ALIASES = {
    'claude': {'max': 'max_5x', 'team': 'team', 'claude pro': 'pro',
               'claude max 5x': 'max_5x', 'claude max 20x': 'max_20x'},
    'codex': {'pro': 'pro_200', 'team': 'business',
              'chatgpt plus': 'plus', 'chatgpt pro': 'pro_200',
              'chatgpt pro 100': 'pro_100', 'chatgpt pro 200': 'pro_200',
              'chatgpt pro 500': 'pro_500', 'chatgpt business': 'business',
              'self_serve_business_prolite': 'business_premium'},
    'gemini': {'google ai ultra': 'ultra', 'gemini pro': 'pro', 'ultra': 'ultra',
               'google ai ultra 20x': 'ultra', 'ultra 5x': 'ultra_5x',
               'gemini advanced': 'pro', 'google one ai premium': 'pro',
               'ai pro': 'pro', 'ai ultra': 'ultra', 'ultra 20x': 'ultra',
               'g1-pro-tier': 'pro', 'g1-ultra-tier': 'ultra',
               'free-tier': 'free', 'legacy-tier': 'free'},
}
PLAN_PRICES = {}
for _src, _options in PLAN_OPTIONS.items():
    _prices = {}
    for _id, (_name, _price) in _options.items():
        _prices[_plan_key(_id)] = _price
        _prices[_plan_key(_name)] = _price
        _prices[_plan_key(NAMES[_src] + ' ' + _name)] = _price
        if _src == 'gemini':
            _prices[_plan_key(_name.removeprefix('Google AI '))] = _price
    _prices.update({_plan_key(alias): _options[target][1]
                    for alias, target in PLAN_ALIASES.get(_src, {}).items()})
    PLAN_PRICES[_src] = _prices
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
        return lim["accounts"][0].get("plan") or lim.get('plan')
    return lim.get("plan")


def subscriptions(limits, cfg=None):
    """{src: {"usd": float|None, "plan": str|None, "from": "config"|"plan"|None}}"""
    cfg = cfg if cfg is not None else paths.load_config()
    manual = cfg.get("subscription_usd_month") or {}
    plans = cfg.get('subscription_plans') or {}
    selected = {'gemini' if src == 'antigravity' else src for src in enabled(cfg)}
    out = {}
    for src in SOURCES:
        if src not in selected:
            out[src] = {'usd': None, 'plan': None, 'from': None}
            continue
        plan = _plan(limits, src)
        chosen = PLAN_OPTIONS[src].get(plans.get(src))
        if chosen:
            name, price = chosen
            out[src] = {'usd': price, 'plan': name, 'from': 'selection'}
            continue
        m = manual.get(src)
        if isinstance(m, (int, float)) and m > 0:
            out[src] = {"usd": float(m), "plan": plan, "from": "config"}
            continue
        key = _plan_key(plan)
        price = PLAN_PRICES.get(src, {}).get(key)
        out[src] = {"usd": float(price) if price is not None else None, "plan": plan,
                    "from": "plan" if price is not None else None}
    return out


def account_key(account):
    return hashlib.sha256(str(account).encode()).hexdigest()[:12] if account else 'unknown'


def codex_subscriptions(limits, cfg=None):
    cfg = cfg if cfg is not None else paths.load_config()
    accounts = ((limits or {}).get('codex') or {}).get('accounts') or []
    overrides = (cfg.get('account_plans') or {}).get('codex') or {}
    rows, seen = [], set()
    for account in accounts:
        key = account_key(account.get('account'))
        if key in seen:
            continue
        seen.add(key)
        local = dict(cfg)
        local['subscription_plans'] = dict(cfg.get('subscription_plans') or {})
        # A source-level selection describes one subscription, never all accounts.
        local['subscription_plans']['codex'] = overrides.get(key, local['subscription_plans'].get('codex', 'auto') if len(accounts) == 1 else 'auto')
        local['subscription_usd_month'] = dict(cfg.get('subscription_usd_month') or {})
        if len(accounts) > 1:
            local['subscription_usd_month']['codex'] = 0
        sub = subscriptions({'codex': {'plan': account.get('plan')}}, local)['codex']
        rows.append(dict(sub, key=key, label=f'Codex · {key[:4]}', detected_plan=account.get('plan')))
    return rows


def _codex_spend(db, since_ts, cfg):
    if 'codex' not in enabled(cfg):
        return {}
    out = {}
    rows = db.query('SELECT account, model, SUM(input) input, SUM(output) output, '
                    'SUM(cache_write) cache_write, SUM(cache_read) cache_read FROM records '
                    'WHERE source = ? AND ts >= ? AND ts <= ? GROUP BY account, model',
                    ('codex', since_ts, time.time()))
    for row in rows:
        key = account_key(row['account'])
        out[key] = out.get(key, 0) + (cost(row['model'], input=row['input'] or 0, output=row['output'] or 0,
                                         cache_write=row['cache_write'] or 0, cache_read=row['cache_read'] or 0) or 0)
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
    cfg = cfg if cfg is not None else paths.load_config()
    subs = subscriptions(limits, cfg)
    codex = codex_subscriptions(limits, cfg) if 'codex' in enabled(cfg) else []
    if codex:
        subs['codex'] = {'accounts':codex, 'usd':sum(row['usd'] or 0 for row in codex),
                         'plan':' + '.join(row['plan'] or 'Unknown' for row in codex),
                         'from':'accounts', 'partial':any(row['usd'] is None for row in codex)}
    res = {"subscriptions": subs, "periods": {}}
    for period, (win, start, days) in windows().items():
        start_ts = datetime.combine(start, datetime.min.time()).timestamp()
        spend = _spend(db, start_ts, cfg=cfg)
        tools, t_api, t_sub = {}, 0.0, 0.0
        for src in SOURCES:
            if src == 'codex' and codex:
                account_spend = _codex_spend(db, start_ts, cfg)
                known = {entry['key'] for entry in codex}
                entries = codex + [dict(key=key, label='Codex · account unknown' if key == 'unknown' else f'Codex · {key[:4]}',
                                       usd=None, plan=None, **{'from':None}) for key in account_spend if key not in known]
                for entry in entries:
                    usd = entry['usd']
                    share = (usd or 0) * {'day':1/30, 'week':7/30, 'month':1}[period]
                    api = account_spend.get(entry['key'], 0)
                    tools[f"codex:{entry['key']}"] = {'source':'codex','label':entry['label'],
                        'api':round(api,4),'sub':round(share,4),'ratio':api/share if share else None,
                        'plan':entry['plan'],'from':entry['from'],'monthly':usd}
                    if share:
                        t_api += api; t_sub += share
                continue
            usd = subs[src]["usd"]
            share = usd * {'day': 1 / 30, 'week': 7 / 30, 'month': 1}[period] if usd else 0.0
            api = spend[src]
            if not usd and not api:
                continue
            tools[src] = {'source':src,'label':NAMES[src], "api": round(api, 4), "sub": round(share, 4), "ratio": (api / share) if share else None,
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
    cfg = cfg if cfg is not None else paths.load_config()
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
                            "bad token": "Membership expired. Join the group again.",
                            "not found": "The ranking server needs an update"}.get(msg, msg or f"Server error {e.code}"))
    except (urllib.error.URLError, TimeoutError, OSError):
        raise RankingError("Could not connect to the public ranking server. Update Token Tracker and check your connection.")


def _groups(cfg):
    if paths.migrate_ranking(cfg):
        paths.save_config(cfg)
    return [g for g in cfg.get('ranking_groups', []) if g.get('code') and g.get('token')]


def _group(cfg, code=None):
    groups = _groups(cfg)
    if not code:
        return groups[0] if groups else None
    code = str(code).strip().upper()
    if not re.fullmatch(r'[A-Z2-9]{4}-[A-Z2-9]{4}', code):
        raise RankingError('Invite code not found')
    return next((g for g in groups if g['code'] == code), None)


def state(cfg=None):
    cfg = cfg if cfg is not None else paths.load_config()
    return {'server': bool(server_url(cfg)), 'server_url': server_url(cfg),
            'groups': [dict(code=g['code'], group_name=g.get('group_name'), nick=g.get('nick') or '',
                            invite_url=server_url(cfg) + '/?invite=' + g['code'], joined=True,
                            member_count=g.get('member_count')) for g in _groups(cfg)],
            'global': global_state(cfg)}


def _save_group(res, nick):
    cfg = paths.load_config()
    groups = _groups(cfg)
    entry = dict(code=res['code'], group_name=res.get('name'), member_id=res['member_id'],
                 token=res['token'], nick=nick)
    for i, group in enumerate(groups):
        if group['code'] == entry['code']:
            groups[i] = entry
            break
    else:
        groups.append(entry)
    cfg['ranking_groups'] = groups
    paths.save_config(cfg)
    _last_sync['t'] = 0.0


def create(group_name, nick):
    res = _call('POST', '/v1/groups', {'group_name': group_name, 'nick': nick})
    _save_group(res, nick)
    return res


def join(code, nick):
    code = code.strip().upper()
    if not re.fullmatch(r'[A-Z2-9]{4}-[A-Z2-9]{4}', code):
        raise RankingError('Invite code not found')
    cfg = paths.load_config()
    existing = _group(cfg, code)
    if existing:
        try:
            # Reuse a live membership (no duplicate participant), but allow rejoining
            # when the old token has expired or was removed from the server.
            _call('PUT', f'/v1/groups/{code}/score', {'nick': nick, 'periods': {}, 'calculation_version': 2},
                  token=existing['token'], cfg=cfg)
        except RankingError as error:
            if str(error) not in ('bad token', 'Membership expired. Join the group again.'):
                raise
        else:
            existing['nick'] = nick
            paths.save_config(cfg)
            _last_sync['t'] = 0.0
            return dict(existing, name=existing.get('group_name'))
    res = _call('POST', f'/v1/groups/{code}/join', {'nick': nick})
    _save_group(res, nick)
    return res


def leave(code=None):
    cfg = paths.load_config()
    group = _group(cfg, code)
    if not group:
        return
    try:
        _call('DELETE', f"/v1/groups/{group['code']}/me", token=group['token'], cfg=cfg)
    except RankingError:
        pass  # leave locally even if the server is unreachable
    cfg['ranking_groups'] = [g for g in _groups(cfg) if g['code'] != group['code']]
    paths.save_config(cfg)


def set_nick(code=None, nick=None):
    if nick is None:  # pre-multi-group API: set_nick(nick)
        code, nick = None, code
    cfg = paths.load_config()
    group = _group(cfg, code)
    if not group:
        raise RankingError('You have not joined a group yet')
    group['nick'] = nick or ''
    paths.save_config(cfg)
    _last_sync['t'] = 0.0


_lock = threading.Lock()
_last_sync = {"t": 0.0}


def _period_tokens(db):
    """Tokens of every tool since the start of today, this week and this month (local calendar)."""
    out = {}
    for period, (_, start, _) in windows().items():
        ts = datetime.combine(start, datetime.min.time()).timestamp()
        row = db.query("SELECT SUM(input + output + cache_write + cache_read) AS n FROM records WHERE ts >= ?", (ts,))
        out[period] = int((row[0]["n"] if row else 0) or 0)
    return out


def _score_body(db, limits, cfg, nick):
    rep = report(db, limits, cfg)
    plans = ' · '.join(f"{NAMES[t.get('source', s)]} {t['plan'].split('·', 1)[0].strip()}"
                       if t.get('plan') else NAMES[t.get('source', s)]
                       for s, t in rep['periods']['month']['tools'].items() if t['sub'])
    tokens = _period_tokens(db)
    # usage = API-equivalent spend of every tool, even ones without a known subscription price
    return {"nick": nick or "", "plans": plans, 'calculation_version': 2,
            "periods": {p: {"window": v["window"], "api": round(v["api"], 4), "sub": round(v["sub"], 4),
                            "usage": round(sum(t["api"] for t in v["tools"].values()), 4),
                            "tokens": tokens.get(p, 0)}
                        for p, v in rep["periods"].items()}}


def sync(db, limits, force=False):
    """Send this PC's numbers to the group and/or the public ranking. Quiet no-op when not joined
    or synced recently."""
    cfg = paths.load_config()
    groups = _groups(cfg)
    global_member = cfg.get('ranking_global') or {}
    targets = [(f"/v1/groups/{g['code']}/score", g) for g in groups]
    if global_member.get('token'):
        targets.append(('/v1/global/score', global_member))
    if not (targets and server_url(cfg)):
        return False
    with _lock:
        if not force and time.time() - _last_sync['t'] < SYNC_EVERY:
            return False
        body = _score_body(db, limits, cfg, '')
        errors = []
        with ThreadPoolExecutor(max_workers=min(32, len(targets))) as pool:
            futures = [pool.submit(_call, 'PUT', path, dict(body, nick=member.get('nick') or ''),
                                   token=member['token'], cfg=cfg)
                       for path, member in targets]
            # Consume in target order, not completion order, to preserve the first error.
            for future in futures:
                try:
                    future.result()
                except RankingError as error:
                    errors.append(error)  # one expired membership must not block other groups/global
        _last_sync['t'] = time.time()
        if errors:
            raise errors[0]
        return True


# ------------------------------------------------------------------ public ranking
def global_state(cfg=None):
    cfg = cfg if cfg is not None else paths.load_config()
    g = cfg.get("ranking_global") or {}
    groups = cfg.get('ranking_groups') or []
    fallback = groups[0].get('nick') if groups else (cfg.get('ranking') or {}).get('nick')
    return {'joined': bool(g.get('token')), 'nick': g.get('nick') or fallback or ''}


def global_join(nick):
    if global_state()['joined']:
        global_set_nick(nick)
        return {'ok': True}
    res = _call("POST", "/v1/global/join", {"nick": nick})
    cfg = paths.load_config()
    cfg["ranking_global"] = {"member_id": res["member_id"], "token": res["token"], "nick": nick}
    paths.save_config(cfg)
    _last_sync["t"] = 0.0
    return res


def global_leave():
    cfg = paths.load_config()
    g = cfg.get("ranking_global") or {}
    if g.get('token'):
        # Keep the token on connection failures so deletion can be retried. Otherwise
        # the UI would promise erased records while leaving an unreachable orphan.
        try:
            _call('DELETE', '/v1/global/me', token=g['token'], cfg=cfg)
        except RankingError as error:
            if str(error) not in ('bad token', 'Membership expired. Join the group again.'):
                raise
    cfg["ranking_global"] = {}
    paths.save_config(cfg)


def global_set_nick(nick):
    cfg = paths.load_config()
    cfg.setdefault("ranking_global", {})["nick"] = nick
    paths.save_config(cfg)
    _last_sync["t"] = 0.0


def global_ranking(db, limits, period):
    cfg = paths.load_config()
    g = cfg.get("ranking_global") or {}
    if not g.get("token"):
        raise RankingError("Not in the public ranking")
    try:
        sync(db, limits, force=time.time() - _last_sync["t"] > 120)
    except RankingError:
        pass
    period = period if period in PERIODS else "week"
    win = windows()[period][0]
    res = _call("GET", f"/v1/global/ranking?period={period}&window={win}", token=g["token"], cfg=cfg)
    res["nick"] = g.get("nick")
    return res


def ranking(db, limits, period, code=None):
    cfg = paths.load_config()
    r = _group(cfg, code)
    if not r:
        raise RankingError("You have not joined a group yet")
    try:
        sync(db, limits, force=time.time() - _last_sync["t"] > 120)
    except RankingError:
        pass  # show the last ranking even if our own update failed
    period = period if period in PERIODS else "week"
    win = windows()[period][0]
    res = _call("GET", f"/v1/groups/{r['code']}/ranking?period={period}&window={win}", token=r["token"], cfg=cfg)
    res['nick'] = r.get('nick')
    count = len(res.get('members') or [])
    if r.get('member_count') != count or (res.get('name') and r.get('group_name') != res['name']):
        # Re-read after a network call so concurrent create/leave changes are retained.
        latest = paths.load_config()
        current = _group(latest, r['code'])
        if current:
            current['member_count'] = count
            current['group_name'] = res.get('name') or current.get('group_name')
            paths.save_config(latest)
    return res


def random_nick():
    return '사용자-' + secrets.token_hex(2)
