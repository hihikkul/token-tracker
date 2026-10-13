"""Remaining plan limits.

Claude : GET https://api.anthropic.com/api/oauth/usage with the OAuth token Claude Code
         stored at login (~/.claude/.credentials.json). Undocumented endpoint, rate
         limited per token -> polled at most every few minutes, last good value cached.
         When the stored token has expired (common when Claude Code runs only inside the
         desktop app, which keeps its own login), it is refreshed the same way Claude Code
         does it and the new pair is written back to the same file, other fields untouched.
Codex  : read from the rate_limits block Codex writes into its session logs (no network),
         kept per account because one PC can be signed into several.
Gemini : no usage-limit source available.
"""
import json
import os
import time
import urllib.error
import urllib.request

from . import paths
from .collectors import parse_ts

CLAUDE_URL = "https://api.anthropic.com/api/oauth/usage"
# Claude Code's public OAuth client (same values the Claude Code CLI uses to refresh its login)
CLAUDE_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLAUDE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
REFRESH_RETRY_SEC = 1800
# Claude Code's refresh token lasts about 30 days and is replaced on every refresh. Refreshing
# well before it runs out keeps the login alive for as long as the tracker runs now and then,
# even if Claude Code itself is not opened for weeks.
KEEPALIVE_SEC = 7 * 86400


def _read_oauth(cred_file):
    try:
        return json.loads(cred_file.read_text(encoding="utf-8")).get("claudeAiOauth") or {}
    except (OSError, ValueError):
        return {}


def _refresh_claude(cred_file, db):
    """Refresh an expired Claude Code login and write it back. Returns (oauth, error)."""
    now = time.time()
    if now - (db.get_kv("claude_refresh_failed_at") or 0) < REFRESH_RETRY_SEC:
        return None, "recent_failure"
    try:
        raw = json.loads(cred_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, "unreadable"
    oauth = raw.get("claudeAiOauth") or {}
    rt = oauth.get("refreshToken")
    if not rt:
        return None, "no_refresh_token"
    body = {"grant_type": "refresh_token", "refresh_token": rt, "client_id": CLAUDE_CLIENT_ID}
    if oauth.get("scopes"):
        body["scope"] = " ".join(oauth["scopes"])
    req = urllib.request.Request(CLAUDE_TOKEN_URL, data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", "Accept": "application/json",
                                          "User-Agent": "tokentracker/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # Claude Code may have refreshed (and so replaced the refresh token) at the same moment
        fresh = _read_oauth(cred_file)
        if fresh.get("refreshToken") and fresh.get("refreshToken") != rt:
            return fresh, None
        db.set_kv("claude_refresh_failed_at", now)
        return None, ("relogin" if e.code in (400, 401, 403) else f"http_{e.code}")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None, "offline"
    if not data.get("access_token"):
        db.set_kv("claude_refresh_failed_at", now)
        return None, "bad_response"
    oauth["accessToken"] = data["access_token"]
    oauth["refreshToken"] = data.get("refresh_token") or rt
    if data.get("expires_in"):
        oauth["expiresAt"] = int((now + float(data["expires_in"])) * 1000)
    # Claude Code treats the login as gone once refreshTokenExpiresAt has passed, so it must
    # move forward together with the new refresh token (same fields Claude Code writes)
    if data.get("refresh_token_expires_in"):
        oauth["refreshTokenExpiresAt"] = int((now + float(data["refresh_token_expires_in"])) * 1000)
    elif data.get("refresh_token") and data["refresh_token"] != rt:
        oauth["refreshTokenExpiresAt"] = int((now + 30 * 86400) * 1000)
    if isinstance(data.get("scope"), str) and data["scope"].strip():
        oauth["scopes"] = data["scope"].split()
    raw["claudeAiOauth"] = oauth
    tmp = cred_file.with_name(cred_file.name + ".tokentracker.tmp")
    try:
        tmp.write_text(json.dumps(raw), encoding="utf-8")
        os.replace(tmp, cred_file)
    except OSError:
        # the new refresh token must not be lost, or Claude Code would need a new login
        return oauth, "not_saved"
    db.set_kv("claude_refresh_failed_at", 0)
    return oauth, None

CLAUDE_LABELS = {
    "five_hour": ("5h", 1),
    "seven_day": ("Weekly", 2),
    "seven_day_opus": ("Weekly Opus", 3),
    "seven_day_sonnet": ("Weekly Sonnet", 4),
    "seven_day_oauth_apps": ("Weekly Apps", 5),
}


def _window_label(minutes):
    if not minutes:
        return "Limit"
    m = int(minutes)
    if 280 <= m <= 320:
        return "5h"
    if 9900 <= m <= 10200:
        return "Weekly"
    if m % 1440 == 0:
        return f"{m // 1440}d"
    return f"{round(m / 60)}h"


class _Relogin(Exception):
    pass


def _get_usage(token):
    req = urllib.request.Request(CLAUDE_URL, headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
        "Accept": "application/json",
        "User-Agent": "tokentracker-personal/1.0",
    })
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_claude(db, force=False):
    cfg = paths.load_config()
    cached = db.get_kv("claude_limits") or {}
    now = time.time()
    min_gap = cfg.get("limits_interval_sec", 300)
    if cached.get("backoff_until", 0) > now and not force:
        return cached
    if not force and now - cached.get("checked_at", 0) < min_gap:
        return cached

    result = {"source": "claude", "checked_at": now, "windows": cached.get("windows", []),
              "fetched_at": cached.get("fetched_at"), "status": "ok", "message": ""}
    cred_file = paths.claude_credentials_file()
    keychain = None if cred_file else paths.claude_keychain_oauth()
    if not cred_file and keychain is None:
        result.update(status="no_credentials",
                      message="Claude Code login credentials were not found")
        db.set_kv("claude_limits", result)
        return result
    if keychain is not None:
        oauth = keychain
    else:
        try:
            oauth = json.loads(cred_file.read_text(encoding="utf-8")).get("claudeAiOauth") or {}
        except (OSError, ValueError):
            oauth = {}
    token = oauth.get("accessToken")
    sub = (oauth.get("subscriptionType") or "").lower()
    if sub:
        result["plan"] = {"max": "Max", "pro": "Pro", "team": "Team", "enterprise": "Enterprise",
                          "free": "Free"}.get(sub, sub.title())
        tier = str(oauth.get("rateLimitTier") or "")
        if "20x" in tier:
            result["plan"] += " 20x"
        elif "5x" in tier:
            result["plan"] += " 5x"
    exp = oauth.get("expiresAt")
    rt_exp = oauth.get("refreshTokenExpiresAt")
    refresh_note = None
    can_refresh = bool(cred_file) and cfg.get("refresh_claude_token", True)
    access_due = bool(exp) and exp / 1000 < now + 60
    keepalive_due = bool(rt_exp) and rt_exp / 1000 < now + KEEPALIVE_SEC
    if token and can_refresh and (access_due or keepalive_due):
        new, err = _refresh_claude(cred_file, db)
        if new:
            token, exp = new["accessToken"], new.get("expiresAt")
            rt_exp = new.get("refreshTokenExpiresAt") or rt_exp
        refresh_note = err
    if rt_exp:
        result["login_until"] = rt_exp / 1000
    if not token:
        result.update(status="no_credentials", message="No OAuth token (API key login?)")
    elif exp and exp / 1000 < now:
        if refresh_note == "relogin" or (rt_exp and rt_exp / 1000 < now):
            result.update(status="relogin", message="Claude sign-in ended. Right-click → Sign in to Claude")
        else:
            msg = {"offline": "Token refresh failed (check your connection)"}.get(
                refresh_note, "Login expired. Retrying shortly")
            result.update(status="expired", message=msg)
    else:
        try:
            try:
                data = _get_usage(token)
            except urllib.error.HTTPError as e:
                if e.code not in (401, 403):
                    raise
                # the token was revoked early (e.g. replaced elsewhere): refresh once and retry
                new, err = _refresh_claude(cred_file, db) if can_refresh else (None, "relogin")
                if not new:
                    raise _Relogin(err)
                data = _get_usage(new["accessToken"])
            windows = []
            for key, v in data.items():
                if not isinstance(v, dict) or v.get("utilization") is None:
                    continue
                label, order = CLAUDE_LABELS.get(key, (key.replace("_", " "), 9))
                windows.append({"key": key, "label": label, "order": order,
                                "used_percent": float(v["utilization"]),
                                "resets_at": parse_ts(v.get("resets_at"))})
            windows.sort(key=lambda w: w["order"])
            result.update(windows=windows, fetched_at=now)
            extra = data.get("extra_usage")
            if isinstance(extra, dict) and extra.get("is_enabled"):
                result["extra"] = {k: extra.get(k) for k in
                                   ("monthly_limit", "used_credits", "utilization")}
        except _Relogin as e:
            if str(e) == "relogin":
                result.update(status="relogin", message="Claude sign-in ended. Right-click → Sign in to Claude")
            else:
                result.update(status="auth", message="Couldn't renew the Claude sign-in. Retrying later")
        except urllib.error.HTTPError as e:
            if e.code == 429:
                result.update(status="rate_limited", backoff_until=now + 900,
                              message="Rate limited (showing cached values)")
            elif e.code in (401, 403):
                result.update(status="relogin", message="Claude sign-in ended. Right-click → Sign in to Claude")
            else:
                result.update(status="error", message=f"HTTP {e.code}")
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            result.update(status="offline", message=f"Connection failed: {getattr(e, 'reason', e)}")
    db.set_kv("claude_limits", result)
    return result


PLAN_NAMES = {"plus": "Plus", "pro": "Pro", "team": "Team", "business": "Business",
              "self_serve_business_prolite": "Business Lite", "enterprise": "Enterprise",
              "free": "Free", "edu": "Edu"}


def _codex_one(snap, now):
    out = {"windows": [], "plan": PLAN_NAMES.get(snap.get("plan"), snap.get("plan")),
           "fetched_at": snap.get("ts"), "account": snap.get("account")}
    for w in snap["windows"]:
        reset = w.get("resets_at")
        stale = bool(reset and reset < now)  # window already rolled over since last use
        out["windows"].append({
            "key": w["key"], "label": _window_label(w.get("window_minutes")),
            "used_percent": 0.0 if stale else w["used_percent"],
            "resets_at": None if stale else reset,
            "note": "Reset" if stale else "",
        })
    order = {"5h": 0, "Weekly": 1}
    out["windows"].sort(key=lambda w: order.get(w["label"], 5))
    return out


def codex_limits(db):
    snaps = list((db.get_kv("codex_limits_by_account") or {}).values())
    out = {"source": "codex", "windows": [], "accounts": [], "status": "ok", "message": ""}
    if not snaps:
        out.update(status="no_data", message="No quota information in Codex logs yet")
        return out
    now = time.time()
    if any(s.get("account") for s in snaps):
        snaps = [s for s in snaps if s.get("account")]  # old logs carried no account id
    snaps.sort(key=lambda s: -(s.get("ts") or 0))
    # Two snapshots belong to the same quota when their windows reset at the same moments
    # (a plan-less snapshot from newer Codex builds and a labelled one, or old keying).
    # Keep the newest values and borrow the plan name from any snapshot in the group.
    def resets(s):
        return {w.get("window_minutes"): w.get("resets_at") for w in s.get("windows", [])
                if w.get("resets_at") and w.get("window_minutes")}

    def same_quota(a, b):  # judged on the longest window both have (the weekly one)
        ra, rb = resets(a), resets(b)
        common = [m for m in ra if m in rb]
        return bool(common) and abs(ra[max(common)] - rb[max(common)]) <= 120

    uniq = []
    for s in snaps:  # newest first
        hit = next((u for u in uniq if u.get("limit_id") == s.get("limit_id")
                    and u.get('account') == s.get('account')
                    and ((s.get("plan") and s.get("plan") == u.get("plan")) or same_quota(u, s))), None)
        if hit is None:
            uniq.append(dict(s))
        elif not hit.get("plan") and s.get("plan"):
            hit["plan"] = s["plan"]
    for u in uniq:
        if not u.get("plan") and u.get("plan_guess"):
            u["plan"] = u["plan_guess"]
    # an older entry with the same plan name is a stale copy of a newer one
    seen, kept = set(), []
    for u in uniq:
        k = (u.get("account") or u.get("plan"), u.get("limit_id"))
        if k not in seen:
            seen.add(k); kept.append(u)
    uniq = kept
    accts = [_codex_one(s, now) for s in uniq]
    # same plan twice -> add a short account suffix so they can be told apart
    plans = [a["plan"] for a in accts]
    for a in accts:
        if plans.count(a["plan"]) > 1 and a.get("account"):
            a["plan"] = f"{a['plan']} ·{a['account'][:4]}"
    out["accounts"] = accts
    out["windows"] = accts[0]["windows"]          # most recently used account first
    out["plan"] = accts[0]["plan"]
    out["fetched_at"] = accts[0]["fetched_at"]
    age = now - (accts[0]["fetched_at"] or now)
    if age > 3600:
        h = int(age // 3600)
        out["message"] = f"Last Codex usage ({h}h ago)" if h < 48 else f"Last Codex usage ({h // 24}d ago)"
    return out


def all_limits(db, force=False):
    from .sources import enabled
    from .google_limits import fetch
    providers = {
        'claude': lambda: fetch_claude(db, force=force),
        'codex': lambda: codex_limits(db),
        'gemini': lambda: fetch(db, 'gemini', force),
        'antigravity': lambda: fetch(db, 'antigravity', force),
    }
    out = {}
    for source in enabled():
        try:
            if source in providers:
                out[source] = providers[source]()
            else:
                out[source] = {'source': source, 'windows': [], 'status': 'unsupported',
                    'message': 'Web token usage and model quotas cannot be fetched automatically. CLI usage is separate.'}
        except Exception:
            out[source] = {'source': source, 'windows': [], 'status': 'error',
                           'message': 'Could not fetch limits for this app'}
    return out
