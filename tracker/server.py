"""Local-only HTTP server (127.0.0.1) for the dashboard."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import export, paths, pricing, ranking, sources, stats, updater
from .sources import enabled
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
LOOPBACK_NAMES = ("127.0.0.1", "localhost", "[::1]")


def make_handler(engine):
    cache_key, cached = None, None
    cache_lock = threading.Lock()

    def dashboard_data():
        nonlocal cache_key, cached
        selected = set(enabled())
        names = frozenset(name for key, name in sources.names().items() if key in selected)
        with cache_lock:
            now = time.time()
            sig = engine.db.query('SELECT (SELECT COUNT(*) FROM records) AS n, '
                                  '(SELECT MAX(ts) FROM records WHERE ts<=?) AS last', (now,))[0]
            key = (sig['n'], sig['last'], engine.db.record_generation, pricing.generation, names)
            if key != cache_key:
                data = export.dashboard_rows(engine.db, now)
                for field in ('daily_detail', 'hours_detail'):
                    data[field] = [r for r in data[field] if r['tool'] in names]
                models = {r['model'] for r in data['daily_detail']}
                data['prices'] = {m: p for m, p in data['prices'].items() if m in models}
                cached, cache_key = data, key
            snap = engine.snapshot
            meta = {'last_scan': snap.get('updated'), 'source_meta': sources.meta()}
            if not cached['daily_detail']:
                meta['selected_tools'] = sorted(names)
                meta['checked_dirs'] = {s: r.get('dirs', []) for s, r in (snap.get('scan') or {}).items()
                                        if isinstance(r, dict)}
            return dict(cached, meta=meta)

    class H(BaseHTTPRequestHandler):
        timeout = 15

        def log_message(self, *a):
            pass

        def _local_host(self):
            """The request's Host when it names this server on the loopback, else None.

            Web pages cannot read 127.0.0.1 themselves, but DNS rebinding can point their own
            domain here; those requests still carry that domain in Host, so they are refused."""
            host = (self.headers.get("Host") or "").strip().lower()
            port = self.server.server_address[1]
            return host if host in {f"{name}:{port}" for name in LOOPBACK_NAMES} else None

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
            if not self._local_host():
                return self._send(403, {"error": "forbidden"})
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path == '/api/settings':
                cfg = paths.load_config()
                names = sources.names()
                selected = set(enabled(cfg))
                return self._send(200, {'sources': {k: k in selected for k in names},
                    'source_names': names, 'source_meta': sources.meta(), 'version': updater.local_version(),
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
            if u.path == '/api/ranking/state':
                return self._send(200, ranking.state())
            if u.path == "/api/ranking/global":
                try:
                    res = ranking.global_ranking(engine.db, ranking_limits(engine), (q.get("period") or ["week"])[0])
                except ranking.RankingError as e:
                    return self._send(200, {"error": str(e), "state": ranking.global_state()})
                res["state"] = ranking.global_state()
                return self._send(200, res)
            if u.path == "/api/ranking":
                try:
                    res = ranking.ranking(engine.db, ranking_limits(engine), (q.get("period") or ["week"])[0],
                                          (q.get('code') or [None])[0])
                except ranking.RankingError as e:
                    return self._send(200, {"error": str(e), "state": ranking.state()})
                res["state"] = ranking.state()
                return self._send(200, res)
            if u.path in ("/favicon.png", "/favicon.ico"):
                return self._send(200, (paths.APP_DIR / "assets" / "token-tracker.png").read_bytes(), "image/png")
            if u.path == "/api/data":
                return self._send(200, dashboard_data())
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
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if n < 0 or n > 16384:
                self.close_connection = True
                self._send(413 if n > 16384 else 400, {'error': 'invalid Content-Length'})
                return None
            try:
                body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
                if not isinstance(body, dict):
                    raise ValueError('expected JSON object')
                return body
            except ValueError:
                self._send(400, {'error': 'invalid JSON body'})
            except OSError:
                self.close_connection = True
                self._send(408, {'error': 'request body timed out'})
            return None

        def do_POST(self):
            path = urlparse(self.path).path
            host = self._local_host()
            if not host or self.headers.get("Origin") not in (None, f"http://{host}"):
                return self._send(403, {"error": "forbidden"})  # only the dashboard itself may change settings
            b = self._body()
            if b is None:
                return
            if path == '/api/update':
                try:
                    latest = updater.check()
                    mode = 'download' if updater.is_frozen() else 'install'  # git clones update with git pull
                    instructions = None
                    if not getattr(engine, 'update_callback', None):
                        mode = 'manual'
                        instructions = ('앱을 종료한 뒤 설치 폴더에서 git pull --ff-only를 실행하고 다시 시작하세요.'
                                        if updater.is_git_checkout() else '새 버전을 내려받아 앱을 종료한 뒤 설치하세요.')
                    elif latest and mode == 'install':
                        engine.update_callback(latest)
                    return self._send(200, {'current': updater.local_version(), 'latest': latest, 'instructions': instructions,
                        'mode': mode, 'url': 'https://github.com/hihikkul/token-tracker/releases/latest'})
                except Exception:
                    return self._send(503, {'error': 'Update check failed. Check your internet connection.'})
            if path.startswith("/api/ranking/"):
                nick = str(b.get("nick") or "").strip()[:24]
                joined = None
                try:
                    if path == "/api/ranking/global/join":
                        ranking.global_join(nick or ranking.random_nick())
                    elif path == "/api/ranking/global/leave":
                        ranking.global_leave()
                    elif path == "/api/ranking/global/nick":
                        ranking.global_set_nick(nick or ranking.random_nick())
                    elif path == "/api/ranking/create":
                        joined = ranking.create(str(b.get("group_name") or "").strip()[:40] or "token-tracker",
                                       nick or ranking.random_nick())
                    elif path == "/api/ranking/join":
                        joined = ranking.join(str(b.get("code") or ""), nick or ranking.random_nick())
                    elif path == "/api/ranking/leave":
                        ranking.leave(b.get('code'))
                    elif path == "/api/ranking/nick":
                        ranking.set_nick(b.get('code'), nick or ranking.random_nick())
                    else:
                        return self._send(404, {"error": "not found"})
                except ranking.RankingError as e:
                    return self._send(200, {"error": str(e), "state": ranking.state()})
                if path in ("/api/ranking/create", "/api/ranking/join", "/api/ranking/nick", "/api/ranking/global/join", "/api/ranking/global/nick"):
                    try:
                        ranking.sync(engine.db, ranking_limits(engine), force=True)
                    except ranking.RankingError:
                        pass
                return self._send(200, {'ok': True, 'state': ranking.state(),
                                        **({'code': joined['code']} if joined else {})})
            if path == "/api/settings":
                with paths.config_lock():  # nothing else may save between this read and the write
                    cfg = paths.load_config()
                    selected = b.get('sources')
                    if isinstance(selected, dict):
                        cfg['sources'] = {key: value for key, value in selected.items()
                                          if key in sources.names() and isinstance(value, bool)}
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
                            if key in valid and isinstance(choice, str) and (choice in ('auto', 'excluded') or choice in ranking.PLAN_OPTIONS['codex']):
                                choices[key] = choice
                    prices = b.get("subscription_usd_month")
                    for src, v in (prices if isinstance(prices, dict) else {}).items():
                        if src in ranking.SOURCES and isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v < 10000:
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
                    if not paths.save_config(cfg):
                        return self._send(500, {'error': '설정을 저장하지 못했습니다. 디스크 공간과 쓰기 권한을 확인하세요.'})
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
