"""Claude login renewal: keep-alive, revoked token retry, rejected refresh, race with Claude Code."""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class _Server(BaseHTTPRequestHandler):
    mode = {}

    def log_message(self, *a):
        pass

    def _send(self, code, obj=None):
        self.send_response(code)
        self.end_headers()
        if obj is not None:
            self.wfile.write(json.dumps(obj).encode())

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        if self.mode.get("reject"):
            if self.mode.get("race"):
                self.mode["race"]()
            return self._send(400, {"error": "invalid_grant"})
        self._send(200, {"access_token": "new", "refresh_token": "r2", "expires_in": 28800,
                         "refresh_token_expires_in": 2592000, "scope": "user:inference user:profile"})

    def do_GET(self):
        if self.headers["Authorization"].split()[1] in self.mode.get("revoked", ()):
            return self._send(401)
        self._send(200, {"five_hour": {"utilization": 12.0, "resets_at": None}})


class ClaudeLogin(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), _Server)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        (root / "claude").mkdir()
        cls.cred = root / "claude" / ".credentials.json"
        os.environ.update(TOKENTRACKER_HOME=str(root / "data"), CLAUDE_CONFIG_DIR=str(root / "claude"),
                          NO_PROXY="127.0.0.1")
        from tracker import limits, db
        cls.limits = limits
        port = cls.srv.server_address[1]
        limits.CLAUDE_TOKEN_URL = f"http://127.0.0.1:{port}/token"
        limits.CLAUDE_URL = f"http://127.0.0.1:{port}/usage"
        cls.db = db.DB(Path(cls.tmp.name) / "t.db")

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.tmp.cleanup()

    def write(self, **over):
        o = {"accessToken": "old", "refreshToken": "r1", "expiresAt": int((time.time() + 3600) * 1000),
             "refreshTokenExpiresAt": int((time.time() + 20 * 86400) * 1000),
             "scopes": ["user:inference", "user:profile"], "subscriptionType": "pro"}
        o.update(over)
        self.cred.write_text(json.dumps({"claudeAiOauth": o, "mcpOAuth": {"keep": 1}}))

    def fetch(self):
        _Server.mode = self.mode
        self.db.set_kv("claude_refresh_failed_at", 0)
        r = self.limits.fetch_claude(self.db, force=True)
        raw = json.loads(self.cred.read_text())
        return r, raw["claudeAiOauth"], raw

    def setUp(self):
        self.mode = {}

    def test_expired_access_token_is_renewed_with_its_expiry_fields(self):
        self.write(expiresAt=int((time.time() - 60) * 1000))
        r, o, raw = self.fetch()
        self.assertEqual(r["status"], "ok")
        self.assertEqual((o["accessToken"], o["refreshToken"]), ("new", "r2"))
        self.assertGreater(o["refreshTokenExpiresAt"] / 1000, time.time() + 29 * 86400)
        self.assertEqual(raw["mcpOAuth"], {"keep": 1})

    def test_keepalive_before_refresh_token_runs_out(self):
        self.write(refreshTokenExpiresAt=int((time.time() + 3 * 86400) * 1000))
        r, o, _ = self.fetch()
        self.assertEqual(o["refreshToken"], "r2")

    def test_revoked_token_is_renewed_and_retried(self):
        self.write()
        self.mode["revoked"] = ("old",)
        r, o, _ = self.fetch()
        self.assertEqual((r["status"], len(r["windows"])), ("ok", 1))

    def test_rejected_refresh_asks_for_sign_in(self):
        self.write(expiresAt=int((time.time() - 60) * 1000))
        self.mode["reject"] = True
        r, o, _ = self.fetch()
        self.assertEqual(r["status"], "relogin")
        self.assertEqual(o["refreshToken"], "r1")

    def test_refresh_race_with_claude_code_uses_its_new_login(self):
        self.write(expiresAt=int((time.time() - 60) * 1000))
        self.mode.update(reject=True, race=lambda: self.write(accessToken="cc", refreshToken="r9"))
        r, o, _ = self.fetch()
        self.assertEqual(r["status"], "ok")
        self.assertEqual(o["refreshToken"], "r9")


if __name__ == "__main__":
    unittest.main()
