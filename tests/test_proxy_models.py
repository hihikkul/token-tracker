"""GPT and other models answering inside Claude Code (e.g. through ClaudeRipple) are not Claude usage."""
import json
import time
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class IsolatedEnvironment(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tracker.collectors import claude_log_source  # noqa: E402


class ProxyModels(IsolatedEnvironment):
    def test_vendor_rules(self):
        cases = {
            "claude-opus-5-5": "claude", "claude-haiku-5-5": "claude", "anthropic/claude-opus-5": "claude",
            "gpt-6-astra": "codex", "gpt-6-astra@high": "codex", "gpt-6.1-sol": "codex", "o3": "codex",
            "codex-mini": "codex", "gemini-3-pro": "gemini", "deepseek-v4": "other", "kimi-k2.5": "other",
            "openrouter/openai/gpt-5": "other", "gpt-oss-120b": "other",
        }
        for model, want in cases.items():
            self.assertEqual(claude_log_source(model), want, model)

    def test_transcript_and_old_records_are_split(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proj = root / "claude" / "projects" / "C--work"
            proj.mkdir(parents=True)
            lines = []
            for i, model in enumerate(("claude-opus-5-5", "gpt-6-astra@high", "deepseek-v4")):
                lines.append(json.dumps({"type": "assistant", "timestamp": "2026-10-12T03:00:0%dZ" % i,
                                         "requestId": f"r{i}", "cwd": "C:\\\\work", "sessionId": "s",
                                         "message": {"id": f"m{i}", "model": model,
                                                     "usage": {"input_tokens": 10, "output_tokens": 5}}}))
            (proj / "s.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
            os.environ.update(TOKENTRACKER_HOME=str(root / "data"), CLAUDE_CONFIG_DIR=str(root / "claude"))
            from tracker import collectors, db
            d = db.DB(':memory:')
            # a record stored by an older version under Claude
            d.upsert_records([{"uid": "c:old:1", "source": "claude", "ts": 1, "model": "gpt-6.1-sol",
                               "input": 1, "output": 1}])
            collectors.scan_claude(d, {})
            got = {r["model"]: r["source"] for r in d.query("SELECT model, source FROM records")}
            self.assertEqual(got, {"claude-opus-5-5": "claude", "gpt-6-astra@high": "codex",
                                   "deepseek-v4": "other", "gpt-6.1-sol": "codex"})


if __name__ == "__main__":
    unittest.main()


class UpdaterRoot(unittest.TestCase):
    def test_exe_beside_source_updates_the_source_folder(self):
        from unittest import mock
        from tracker import updater
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "app.py").write_text("")
            Path(tmp, "VERSION").write_text("1.0.0")
            with mock.patch.object(sys, "frozen", True, create=True), \
                    mock.patch.object(sys, "executable", str(Path(tmp, "TokenTracker.exe"))):
                self.assertFalse(updater.is_frozen())
                self.assertEqual(updater.local_version(), "1.0.0")
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(sys, "frozen", True, create=True), \
                    mock.patch.object(sys, "executable", str(Path(tmp, "Token Tracker"))):
                self.assertTrue(updater.is_frozen())


class ProxyAccount(IsolatedEnvironment):
    def test_gpt_turns_get_clauderipples_first_usable_login(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ripple = root / "ripple"
            ripple.mkdir()
            (ripple / "chatgpt-accounts.json").write_text(json.dumps({"accounts": [
                {"id": "x", "accountId": "acct-paused", "paused": True},
                {"id": "y", "accountId": "acct-business"},
                {"id": "z", "accountId": "acct-plus"}]}))
            proj = root / "claude" / "projects" / "C--work"
            proj.mkdir(parents=True)
            (proj / "s.jsonl").write_text(json.dumps({
                "type": "assistant", "timestamp": "2026-10-14T03:00:00Z", "requestId": "r", "sessionId": "s",
                "message": {"id": "m", "model": "gpt-6-astra", "usage": {"input_tokens": 9, "output_tokens": 1}}}) + "\n")
            os.environ.update(TOKENTRACKER_HOME=str(root / "data"), CLAUDE_CONFIG_DIR=str(root / "claude"),
                              CLAUDERIPPLE_HOME=str(ripple), CODEX_HOME=str(root / "codex"))
            from tracker import collectors, db
            d = db.DB(':memory:')
            d.upsert_records([{"uid": "c:old:2", "source": "codex", "ts": 1, "model": "gpt-6.1-sol",
                               "input": 1, "output": 1}])
            collectors.scan_claude(d, {})
            got = {r["uid"]: r["account"] for r in d.query("SELECT uid, account FROM records")}
            self.assertEqual(got, {"c:old:2": "acct-business", "c:m:r": "acct-business"})

    def test_without_clauderipple_the_codex_login_is_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "codex").mkdir()
            (root / "codex" / "auth.json").write_text(json.dumps({"tokens": {"account_id": "acct-codex"}}))
            os.environ.update(CLAUDERIPPLE_HOME=str(root / "none"), CODEX_HOME=str(root / "codex"))
            from tracker.collectors import proxy_chatgpt_account
            self.assertEqual(proxy_chatgpt_account(), "acct-codex")


class RippleQuota(IsolatedEnvironment):
    def test_live_quota_of_the_account_in_use_leads(self):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        now = time.time()
        body = {"provider": "chatgpt-subscription-2", "accounts": [
            {"id": "own-biz", "planType": "self_serve_business_prolite", "active": True,
             "quota": {"plan_type": "self_serve_business_prolite", "at": now * 1000,
                       "rate_limits": {"primary": {"used_percent": 31, "window_minutes": 10080,
                                                   "reset_after_seconds": 3600}, "secondary": None}}},
            {"id": "own-plus", "planType": "plus", "active": False, "quota": None}]}

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(body).encode())

        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ripple = root / "ripple"
            ripple.mkdir()
            (ripple / "config.json").write_text(json.dumps({"admin": {"port": srv.server_address[1]}}))
            (ripple / "chatgpt-accounts.json").write_text(json.dumps({"accounts": [
                {"id": "own-biz", "accountId": "acct-biz"}, {"id": "own-plus", "accountId": "acct-plus"}]}))
            os.environ.update(TOKENTRACKER_HOME=str(root / "data"), CLAUDERIPPLE_HOME=str(ripple),
                              CODEX_HOME=str(root / "codex"))
            from tracker import db, limits
            d = db.DB(':memory:')
            # the Codex CLI last reported the Plus account an hour ago
            d.set_kv("codex_limits_by_account", {"plan:plus:codex": {
                "ts": now - 3600, "limit_id": "codex", "plan": "plus", "account": "acct-plus",
                "windows": [{"key": "primary", "used_percent": 20, "window_minutes": 300, "resets_at": now + 600}]}})
            out = limits.codex_limits(d)
            srv.shutdown()
            srv.server_close()
            d.conn.close()
        self.assertEqual(out["accounts"][0]["account"], "acct-biz")
        self.assertEqual(out["accounts"][0]["plan"], "Business Premium")
        self.assertEqual(out["windows"][0]["used_percent"], 31.0)
        self.assertEqual({a["account"] for a in out["accounts"]}, {"acct-biz", "acct-plus"})
