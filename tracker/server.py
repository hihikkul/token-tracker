"""Local-only HTTP server (127.0.0.1) for the dashboard."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import paths, stats
from .limits import all_limits

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
            if u.path in ("/", "/index.html"):
                return self._send(200, (WEB / "dashboard.html").read_bytes(), "text/html; charset=utf-8")
            if u.path == "/d3.v7.min.js":
                return self._send(200, (WEB / "d3.v7.min.js").read_bytes(), "text/javascript; charset=utf-8")
            if u.path == "/api/data":
                from .export import build
                data = build(engine.db)
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

        def do_POST(self):
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
    if background:
        threading.Thread(target=httpd.serve_forever, daemon=True, name="http").start()
    else:
        print(f"대시보드: {url}")
        httpd.serve_forever()
    return httpd, url
