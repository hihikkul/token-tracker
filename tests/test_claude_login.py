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
        sent = json.loads(self.rfile.read(int(self.headers["Content-Length"])))["refresh_token"]
        reused = sent in self.mode.get("seen", [])  # refresh tokens are single-use when "rotate"
        self.mode.setdefault("seen", []).append(sent)
        if self.mode.get("reject") or (self.mode.get("rotate") and reused):
            if self.mode.get("race"):
                self.mode["race"]()
            return self._send(400, {"error": "invalid_grant"})
        if self.mode.get("during"):  # Claude Code writes its file while this refresh is in flight
            self.mode["during"]()
        n = len(self.mode["seen"]) + 1
        self._send(200, {"access_token": f"new{n - 1}" if n > 2 else "new", "refresh_token": f"r{n}",
                         "expires_in": 28800, "refresh_token_expires_in": 2592000,
                         "scope": "user:inference user:profile"})

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
        from unittest.mock import patch
        cls.env = patch.dict(os.environ, TOKENTRACKER_HOME=str(root / 'data'),
                             CLAUDE_CONFIG_DIR=str(root / 'claude'), NO_PROXY='127.0.0.1')
        cls.env.start()
        from tracker import limits, db
        cls.limits = limits
        port = cls.srv.server_address[1]
        cls.old_urls = (limits.CLAUDE_TOKEN_URL, limits.CLAUDE_URL)
        limits.CLAUDE_TOKEN_URL = f"http://127.0.0.1:{port}/token"
        limits.CLAUDE_URL = f"http://127.0.0.1:{port}/usage"
        cls.db = db.DB(Path(cls.tmp.name) / "t.db")

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.db.conn.close()
        cls.limits.CLAUDE_TOKEN_URL, cls.limits.CLAUDE_URL = cls.old_urls
        cls.env.stop()
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
        getattr(self.limits, "_pending", {}).clear()

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

    def test_unsaved_renewal_is_kept_and_saved_later(self):
        # The refresh spends r1. If the file cannot be replaced right then, the next round must
        # use r2 from memory (never r1 again) and write it once the file is free.
        from unittest.mock import patch
        self.write(expiresAt=int((time.time() - 60) * 1000))
        self.mode["rotate"] = True
        real_replace = os.replace

        def locked(src, dst):
            if Path(dst) == self.cred:
                raise PermissionError("in use")
            return real_replace(src, dst)

        with patch.object(self.limits.os, "replace", side_effect=locked):
            r, o, _ = self.fetch()
        self.assertEqual(r["status"], "ok")
        self.assertIn("could not be saved", r["message"])
        self.assertEqual(o["refreshToken"], "r1")  # file untouched while locked
        r, o, raw = self.fetch()
        self.assertEqual(r["status"], "ok")
        self.assertEqual((o["accessToken"], o["refreshToken"]), ("new", "r2"))
        self.assertEqual(raw["mcpOAuth"], {"keep": 1})
        self.assertEqual(self.mode["seen"], ["r1"])  # the spent token was not sent again
        self.assertEqual(self.limits._pending, {})

    def test_changes_made_during_refresh_are_kept(self):
        self.write(expiresAt=int((time.time() - 60) * 1000))

        def claude_code_adds_mcp_login():
            raw = json.loads(self.cred.read_text())
            raw["mcpOAuth"] = {"keep": 1, "added": 2}
            self.cred.write_text(json.dumps(raw))

        self.mode["during"] = claude_code_adds_mcp_login
        r, o, raw = self.fetch()
        self.assertEqual(o["refreshToken"], "r2")
        self.assertEqual(raw["mcpOAuth"], {"keep": 1, "added": 2})

    def test_claude_code_renewing_during_refresh_wins(self):
        self.write(expiresAt=int((time.time() - 60) * 1000))
        self.mode["during"] = lambda: self.write(accessToken="cc", refreshToken="r9")
        r, o, _ = self.fetch()
        self.assertEqual(r["status"], "ok")
        self.assertEqual((o["accessToken"], o["refreshToken"]), ("cc", "r9"))

    @unittest.skipIf(os.name == "nt", "file modes are POSIX only")
    def test_private_file_mode_is_kept(self):
        self.write(expiresAt=int((time.time() - 60) * 1000))
        os.chmod(self.cred, 0o600)
        self.fetch()
        self.assertEqual(os.stat(self.cred).st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
