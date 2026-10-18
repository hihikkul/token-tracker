"""Local-only HTTP server (127.0.0.1) for the dashboard."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import paths, ranking, stats, updater
from .sources import NAMES, enabled
from .limits import all_limits
from .db import DB


def ranking_limits(engine):
    snapshot = engine.snapshot if isinstance(engine.snapshot, dict) else {}
    result = dict(snapshot.get('limits') or {})
    if 'codex' not in result and isinstance(engine.db, DB):
        from .limits import codex_limits
        result['codex'] = codex_limits(engine.db)
    return result

WEB = paths.APP_DIR / "web"


def make_handler(engine):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False, default=list).encode("utf-8")
            elif isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path == '/api/settings':
                cfg = paths.load_config()
                return self._send(200, {'sources': {k: k in enabled(cfg) for k in NAMES},
                    'source_names': NAMES, 'version': updater.local_version(),
                    'widget': {k: cfg.get('widget', {}).get(k) for k in ('font','number_font','opacity','topmost','taskbar')},
                    'auto_update': cfg.get('auto_update', True),
                    'subscription_usd_month': cfg.get('subscription_usd_month', {}),
                    'subscription_plans': cfg.get('subscription_plans', {}),
                    'plan_options': ranking.PLAN_OPTIONS,
                    'codex_accounts': ranking.codex_subscriptions(ranking_limits(engine), cfg),
                    'account_plans': cfg.get('account_plans', {})})
            if u.path in ("/", "/index.html"):
                return self._send(200, (WEB / "dashboard.html").read_bytes(), "text/html; charset=utf-8")
            if u.path == "/d3.v7.min.js":
                return self._send(200, (WEB / "d3.v7.min.js").read_bytes(), "text/javascript; charset=utf-8")
            if u.path == "/ssalsoongi.jpg":
                return self._send(200, (WEB / "ssalsoongi.jpg").read_bytes(), "image/jpeg")
            if u.path == "/api/ratio":
                rep = ranking.report(engine.db, ranking_limits(engine))
                rep["ranking"] = ranking.state()
                return self._send(200, rep)
            if u.path == "/api/ranking/global":
                try:
                    res = ranking.global_ranking(engine.db, engine.snapshot.get("limits"), (q.get("period") or ["week"])[0])
                except ranking.RankingError as e:
                    return self._send(200, {"error": str(e), "state": ranking.global_state()})
                res["state"] = ranking.global_state()
                return self._send(200, res)
            if u.path == "/api/ranking":
                try:
                    res = ranking.ranking(engine.db, engine.snapshot.get("limits"), (q.get("period") or ["week"])[0])
                except ranking.RankingError as e:
                    return self._send(200, {"error": str(e), "state": ranking.state()})
                res["state"] = ranking.state()
                return self._send(200, res)
            if u.path in ("/favicon.png", "/favicon.ico"):
                return self._send(200, (paths.APP_DIR / "assets" / "token-tracker.png").read_bytes(), "image/png")
            if u.path == "/api/data":
                from .export import build
                data = build(engine.db, limits=engine.snapshot.get('limits') or {})
                selected = set(enabled())
                names = {NAMES[key] for key in selected}
                for key in ('daily', 'daily_detail', 'hours_detail'):
                    data[key] = [row for row in data[key] if row['tool'] in names]
                data["meta"] = {"last_scan": engine.snapshot.get("updated"),
                                "coverage": stats.coverage(engine.db),
                                "artifact_url": paths.load_config().get("artifact_url")}
                return self._send(200, data)
            if u.path == "/api/stats":
                days = max(1, min(365, int((q.get("days") or ["30"])[0])))
                src = (q.get("sources") or [""])[0]
                data = stats.dashboard(engine.db, days=days,
                                       sources=src.split(",") if src else None)
                data["limits"] = engine.snapshot.get("limits") or all_limits(engine.db)
                data["subscription"] = paths.load_config().get("subscription_usd_month", {})
                data["last_scan"] = engine.snapshot.get("updated")
                return self._send(200, data)
            if u.path == "/api/limits":
                return self._send(200, engine.snapshot.get("limits") or {})
            if u.path == "/api/doctor":
                return self._send(200, {"scan": engine.snapshot.get("scan"),
                                        "coverage": stats.coverage(engine.db),
                                        "db": engine.db.path})
            return self._send(404, {"error": "not found"})

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > 16384:
                return {}
            try:
                return json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            except ValueError:
                return {}

        def do_POST(self):
            path = urlparse(self.path).path
            if path.startswith("/api/") and self.headers.get("Origin") not in (None, f"http://{self.headers.get('Host')}"):
                return self._send(403, {"error": "forbidden"})  # only the dashboard itself may change settings
            if path == '/api/update':
                try:
                    latest = updater.check()
                    mode = 'download' if updater.is_frozen() else 'install'  # git clones update with git pull
                    if latest and mode == 'install' and getattr(engine, 'update_callback', None):
                        engine.update_callback(latest)
                    return self._send(200, {'current': updater.local_version(), 'latest': latest,
                        'mode': mode, 'url': 'https://github.com/hihikkul/token-tracker/releases/latest'})
                except Exception:
                    return self._send(503, {'error': 'Update check failed. Check your internet connection.'})
            if path.startswith("/api/ranking/"):
                b = self._body()
                nick = str(b.get("nick") or "").strip()[:24]
                try:
                    if path == "/api/ranking/global/join":
                        ranking.global_join(nick or ranking.random_nick())
                    elif path == "/api/ranking/global/leave":
                        ranking.global_leave()
                    elif path == "/api/ranking/global/nick":
                        ranking.global_set_nick(nick or ranking.random_nick())
                    elif path == "/api/ranking/create":
                        ranking.create(str(b.get("group_name") or "").strip()[:40] or "token-tracker",
                                       nick or ranking.random_nick())
                    elif path == "/api/ranking/join":
                        ranking.join(str(b.get("code") or ""), nick or ranking.random_nick())
                    elif path == "/api/ranking/leave":
                        ranking.leave()
                    elif path == "/api/ranking/nick":
                        ranking.set_nick(nick)
                        ranking.sync(engine.db, engine.snapshot.get("limits"), force=True)
                    else:
                        return self._send(404, {"error": "not found"})
                except ranking.RankingError as e:
                    return self._send(200, {"error": str(e), "state": ranking.state()})
                if path in ("/api/ranking/create", "/api/ranking/join", "/api/ranking/global/join", "/api/ranking/global/nick"):
                    try:
                        ranking.sync(engine.db, engine.snapshot.get("limits"), force=True)
                    except ranking.RankingError:
                        pass
                return self._send(200, {"ok": True, "state": ranking.state()})
            if path == "/api/settings":
                b = self._body()
                cfg = paths.load_config()
                selected = b.get('sources')
                if isinstance(selected, dict):
                    cfg['sources'] = {key: value for key, value in selected.items()
                                      if key in NAMES and isinstance(value, bool)}
                subs = cfg.setdefault("subscription_usd_month", {})
                plans = b.get('subscription_plans')
                if isinstance(plans, dict):
                    choices = cfg.setdefault('subscription_plans', {})
                    for src, choice in plans.items():
                        if src in ranking.PLAN_OPTIONS and isinstance(choice, str) and (choice == 'auto' or choice in ranking.PLAN_OPTIONS[src]):
                            choices[src] = choice
                            subs[src] = 0
                account_plans = b.get('account_plans')
                if isinstance(account_plans, dict) and isinstance(account_plans.get('codex'), dict):
                    valid = {row['key'] for row in ranking.codex_subscriptions(ranking_limits(engine), cfg)}
                    choices = cfg.setdefault('account_plans', {}).setdefault('codex', {})
                    for key, choice in account_plans['codex'].items():
                        if key in valid and isinstance(choice, str) and (choice == 'auto' or choice in ranking.PLAN_OPTIONS['codex']):
                            choices[key] = choice
                prices = b.get("subscription_usd_month")
                for src, v in (prices if isinstance(prices, dict) else {}).items():
                    if src in ("claude", "codex", "gemini") and isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v < 10000:
                        subs[src] = v
                wc = cfg.setdefault('widget', {})
                incoming = b.get('widget')
                incoming = incoming if isinstance(incoming, dict) else {}
                for key in ('topmost', 'taskbar'):
                    if isinstance(incoming.get(key), bool):
                        wc[key] = incoming[key]
                for key in ('font', 'number_font'):
                    if isinstance(incoming.get(key), str) and len(incoming[key]) <= 80:
                        wc[key] = incoming[key].strip()
                opacity = incoming.get('opacity')
                if isinstance(opacity, (int, float)) and not isinstance(opacity, bool) and .2 <= opacity <= 1:
                    wc['opacity'] = opacity
                if isinstance(b.get('auto_update'), bool):
                    cfg['auto_update'] = b['auto_update']
                paths.save_config(cfg)
                engine.poke()
                ranking._last_sync['t'] = 0
                return self._send(200, {"ok": True})
            if urlparse(self.path).path == "/api/refresh":
                snap = engine.refresh(force_limits=True)
                return self._send(200, {"ok": True, "updated": snap["updated"]})
            return self._send(404, {"error": "not found"})

    return H


def serve(engine, port=None, background=True):
    port = port or paths.load_config().get("port", 8765)
    httpd = None
    for p in range(port, port + 10):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", p), make_handler(engine))
            break
        except OSError:
            continue
    if httpd is None:
        raise OSError(f"포트 {port}~{port + 9}가 모두 사용 중이에요")
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    engine.httpd = httpd  # an update closes it first, so the new copy can take the port at once
    if background:
        threading.Thread(target=httpd.serve_forever, daemon=True, name="http").start()
    else:
        print(f"대시보드: {url}")
        httpd.serve_forever()
    return httpd, url
