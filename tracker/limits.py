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
    "five_hour": ("5시간", 1),
    "seven_day": ("주간", 2),
    "seven_day_opus": ("주간 Opus", 3),
    "seven_day_sonnet": ("주간 Sonnet", 4),
    "seven_day_oauth_apps": ("주간 앱", 5),
}


def _window_label(minutes):
    if not minutes:
        return "한도"
    m = int(minutes)
    if 280 <= m <= 320:
        return "5시간"
    if 9900 <= m <= 10200:
        return "주간"
    if m % 1440 == 0:
        return f"{m // 1440}일"
    return f"{round(m / 60)}시간"


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
    if not cred_file:
        result.update(status="no_credentials",
                      message="Claude Code 로그인 정보(.credentials.json)를 찾지 못했어요")
        db.set_kv("claude_limits", result)
        return result
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
    refresh_note = None
    if token and exp and exp / 1000 < now + 60 and cfg.get("refresh_claude_token", True):
        new, err = _refresh_claude(cred_file, db)
        if new:
            token, exp = new["accessToken"], new.get("expiresAt")
        refresh_note = err
    if not token:
        result.update(status="no_credentials", message="OAuth 토큰이 없어요 (API 키 로그인?)")
    elif exp and exp / 1000 < now:
        msg = {"relogin": "로그인이 풀렸어요. Claude Code에서 /login 을 한 번 해 주세요",
               "offline": "토큰 갱신 실패 (인터넷 연결 확인)"}.get(refresh_note, "로그인 토큰 만료. Claude Code를 한 번 실행하면 갱신돼요")
        result.update(status="expired", message=msg)
    else:
        req = urllib.request.Request(CLAUDE_URL, headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "Accept": "application/json",
            "User-Agent": "tokentracker-personal/1.0",
        })
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read().decode("utf-8"))
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
        except urllib.error.HTTPError as e:
            if e.code == 429:
                result.update(status="rate_limited", backoff_until=now + 900,
                              message="조회가 너무 잦아 잠시 쉬는 중 (마지막 값 표시)")
            elif e.code in (401, 403):
                result.update(status="auth", message="인증 실패 — Claude Code 재로그인 필요")
            else:
                result.update(status="error", message=f"HTTP {e.code}")
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            result.update(status="offline", message=f"연결 실패: {getattr(e, 'reason', e)}")
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
            "note": "리셋됨" if stale else "",
        })
    order = {"5시간": 0, "주간": 1}
    out["windows"].sort(key=lambda w: order.get(w["label"], 5))
    return out


def codex_limits(db):
    snaps = list((db.get_kv("codex_limits_by_account") or {}).values())
    out = {"source": "codex", "windows": [], "accounts": [], "status": "ok", "message": ""}
    if not snaps:
        out.update(status="no_data", message="Codex 로그에 한도 정보가 아직 없어요")
        return out
    now = time.time()
    if any(s.get("account") for s in snaps):
        snaps = [s for s in snaps if s.get("account")]  # old logs carried no account id
    snaps.sort(key=lambda s: -(s.get("ts") or 0))
    seen, uniq = set(), []
    for s in snaps:  # one entry per plan, newest wins (older databases keyed by session creator)
        k = (s.get("plan") or s.get("account"), s.get("limit_id"))
        if k not in seen:
            seen.add(k); uniq.append(s)
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
        out["message"] = f"마지막 Codex 사용 기준 ({h}시간 전)" if h < 48 else f"마지막 Codex 사용 기준 ({h // 24}일 전)"
    return out


def all_limits(db, force=False):
    return {
        "claude": fetch_claude(db, force=force),
        "codex": codex_limits(db),
        "gemini": {"source": "gemini", "windows": [], "status": "unsupported",
                   "message": "Gemini는 한도 조회 경로가 없어요"},
    }
