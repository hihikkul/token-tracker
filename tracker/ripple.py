"""Live ChatGPT quotas from a running ClaudeRipple, when there is one.

GPT turns that ClaudeRipple proxies out of Claude Code never reach Codex's session logs, so the
account they spend (often not the one the Codex CLI used last) would show stale limits. ClaudeRipple
keeps the latest quota of every ChatGPT account it holds, read from each response's headers, and
lists them on its local admin API (127.0.0.1, metadata only, no tokens). Account ids come from its
account file; nothing else is read there.
"""
import json
import re
import time
import urllib.request
from datetime import datetime

from . import paths

_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_ROUTER_ACCOUNT = re.compile(r"^(\d\d-\d\d \d\d:\d\d:\d\d) .*\baccount=([0-9a-f]{8})\b")


def _admin_port(home):
    try:
        cfg = json.loads((home / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cfg = {}
    admin = (cfg.get("admin") or {}).get("port")
    return int(admin) if admin else int((cfg.get("listen") or {}).get("port") or 8790) + 1


def _account_ids(home):
    """ClaudeRipple's own account id -> ChatGPT workspace id."""
    out = {}
    try:
        data = json.loads((home / "chatgpt-accounts.json").read_text(encoding="utf-8"))
        for a in (data.get("accounts", []) if isinstance(data, dict) else data) or []:
            if isinstance(a, dict) and a.get("id") and a.get("accountId"):
                out[a["id"]] = a["accountId"]
    except (OSError, ValueError, AttributeError):
        pass
    try:
        tokens = json.loads((paths.codex_root() / "auth.json").read_text(encoding="utf-8")).get("tokens") or {}
        if tokens.get("account_id"):
            out.setdefault("codex", tokens["account_id"])  # the Codex CLI login, as ClaudeRipple names it
    except (OSError, ValueError, AttributeError):
        pass
    return out


def _window(key, w, at):
    if not isinstance(w, dict) or w.get("used_percent") is None:
        return None
    reset = w.get("reset_at")
    if reset is None and w.get("reset_after_seconds") is not None:
        reset = at + float(w["reset_after_seconds"])
    return {"key": key, "used_percent": float(w["used_percent"]),
            "window_minutes": w.get("window_minutes"), "resets_at": float(reset) if reset else None}


def _live_accounts(home, timeout):
    """The accounts on ClaudeRipple's admin API; [] when it is not running."""
    if not home.exists():
        return []
    try:
        url = f"http://127.0.0.1:{_admin_port(home)}/api/chatgpt-accounts"
        with _NO_PROXY.open(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8")).get("accounts") or []
    except (OSError, ValueError, AttributeError):
        return []


def active_account(timeout=1.0):
    """The ChatGPT workspace id ClaudeRipple is sending with right now, if it is running."""
    home = paths.clauderipple_home()
    for a in _live_accounts(home, timeout):
        if a.get("active"):
            return _account_ids(home).get(a.get("id"))
    return None


def account_timeline():
    """(unix time, workspace id) of each request ClaudeRipple's router logs tied to an account,
    oldest first. Log lines carry no year, so a date still ahead of today belongs to last year."""
    home = paths.clauderipple_home()
    ids = {k[:8]: v for k, v in _account_ids(home).items()}
    now = datetime.now()
    out = []
    for log in (home / "logs").glob("router.log*"):
        try:
            f = open(log, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with f:
            for line in f:
                m = _ROUTER_ACCOUNT.match(line)
                if not m or m.group(2) not in ids:
                    continue
                try:
                    at = datetime.strptime(f"{now.year}-{m.group(1)}", "%Y-%m-%d %H:%M:%S")
                    if at > now:
                        at = at.replace(year=now.year - 1)
                except ValueError:
                    continue
                out.append((int(at.timestamp()), ids[m.group(2)]))
    return sorted(out)


def chatgpt_snapshots(timeout=1.0):
    """Quota snapshots shaped like the ones read from Codex logs; [] without ClaudeRipple."""
    home = paths.clauderipple_home()
    accounts = _live_accounts(home, timeout)
    if not accounts:
        return []
    ids = _account_ids(home)
    out = []
    for a in accounts:
        q = a.get("quota") or {}
        rl = q.get("rate_limits") or {}
        acct = ids.get(a.get("id"))
        if not acct or not rl:
            continue
        at = (q.get("at") or time.time() * 1000) / 1000
        wins = [w for w in (_window("primary", rl.get("primary"), at), _window("secondary", rl.get("secondary"), at)) if w]
        if wins:
            out.append({"ts": at, "windows": wins, "limit_id": "codex", "account": acct,
                        "plan": q.get("plan_type") or a.get("planType"), "via": "clauderipple",
                        "active": bool(a.get("active"))})
    return out
