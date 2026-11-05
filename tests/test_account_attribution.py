"""Which account really answered, and how much a proxied turn really used."""
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tracker import collectors, limits, pricing, ripple  # noqa: E402
from tracker.db import DB  # noqa: E402

BIZ, PLUS = "acct-business", "acct-plus"


def assistant_line(mid, model, usage, ts="2026-10-21T01:00:00Z"):
    return json.dumps({"type": "assistant", "timestamp": ts, "requestId": "r", "sessionId": "s",
                       "message": {"id": mid, "model": model, "usage": usage}})


def quota(account, reset, used=50, ts=None):
    return {"ts": ts or time.time(), "limit_id": "codex", "account": account, "windows": [
        {"key": "secondary", "window_minutes": 10080, "resets_at": reset, "used_percent": used}]}


class Isolated(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.ripple_home = self.root / "ripple"
        self.ripple_home.mkdir()
        os.environ.update(TOKENTRACKER_HOME=str(self.root / "data"), CLAUDE_CONFIG_DIR=str(self.root / "claude"),
                          CODEX_HOME=str(self.root / "codex"), CLAUDERIPPLE_HOME=str(self.ripple_home))
        self.db = DB(":memory:")
        self.addCleanup(self.db.conn.close)
        self.transcript = self.root / "claude" / "projects" / "C--work" / "s.jsonl"
        self.transcript.parent.mkdir(parents=True)

    def write_accounts(self):
        (self.ripple_home / "chatgpt-accounts.json").write_text(json.dumps({"accounts": [
            {"id": "11111111-biz", "accountId": BIZ}, {"id": "22222222-plus", "accountId": PLUS}]}))

    def row(self, uid):
        rows = self.db.query("SELECT * FROM records WHERE uid = ?", (uid,))
        return rows[0] if rows else None


class ProxyPlaceholders(Isolated):
    def test_placeholder_never_inflates_the_real_numbers(self):
        self.transcript.write_text("\n".join([
            assistant_line("m1", "gpt-6-luna", {"input_tokens": 3480078, "output_tokens": 0}),
            assistant_line("m1", "gpt-6-luna", {"input_tokens": 743, "cache_read_input_tokens": 158592,
                                                "output_tokens": 479})]) + "\n")
        collectors.scan_claude(self.db, {})
        row = self.row("c:m1:r")
        self.assertEqual((row["input"], row["cache_read"], row["output"]), (743, 158592, 479))

    def test_turn_with_only_a_placeholder_is_not_counted(self):
        self.transcript.write_text(assistant_line("m2", "gpt-6-luna", {"input_tokens": 3956881}) + "\n")
        collectors.scan_claude(self.db, {})
        self.assertIsNone(self.row("c:m2:r"))

    def test_claude_first_line_still_counts(self):
        self.transcript.write_text(assistant_line("m3", "claude-opus-5-5", {"input_tokens": 12}) + "\n")
        collectors.scan_claude(self.db, {})
        self.assertEqual(self.row("c:m3:r")["input"], 12)

    def test_repair_rewrites_stored_records_once(self):
        self.db.upsert_records([
            {"uid": "c:m1:r", "source": "codex", "ts": 1, "model": "gpt-6-luna", "input": 3480078,
             "cache_read": 158592, "output": 479},
            {"uid": "c:m2:r", "source": "codex", "ts": 1, "model": "gpt-6-luna", "input": 3956881},
            {"uid": "c:gone:r", "source": "codex", "ts": 1, "model": "gpt-6-luna", "input": 9, "output": 2},
            {"uid": "c:native:r", "source": "claude", "ts": 1, "model": "claude-opus-5-5", "input": 3},
            {"uid": "x:s:1", "source": "codex", "ts": 1, "model": "gpt-6-luna", "input": 7}])
        self.transcript.write_text("\n".join([
            assistant_line("m1", "gpt-6-luna", {"input_tokens": 3480078}),
            assistant_line("m1", "gpt-6-luna", {"input_tokens": 743, "cache_read_input_tokens": 158592,
                                                "output_tokens": 479}),
            assistant_line("m2", "gpt-6-luna", {"input_tokens": 3956881})]) + "\n")
        collectors.scan_claude(self.db, {})
        self.assertEqual(self.row("c:m1:r")["input"], 743)
        self.assertIsNone(self.row("c:m2:r"))
        self.assertEqual(self.row("c:gone:r")["input"], 9)  # its log is gone: nothing better to go on
        self.assertEqual(self.row("c:native:r")["input"], 3)
        self.assertEqual(self.row("x:s:1")["input"], 7)  # a Codex log's own count, not a proxy's
        self.db.conn.execute("UPDATE records SET input = 5 WHERE uid = 'c:m1:r'")
        collectors.scan_claude(self.db, {})
        self.assertEqual(self.row("c:m1:r")["input"], 5)


class VendorSplit(Isolated):
    def test_turns_stored_under_claude_after_the_first_split_are_moved(self):
        self.db.set_kv("claude_vendor_split", 1)
        self.db.upsert_records([{"uid": "c:late:r", "source": "claude", "ts": 1, "model": "gpt-6-astra",
                                 "input": 1, "output": 1}])
        collectors.scan_claude(self.db, {})
        self.assertEqual(self.row("c:late:r")["source"], "codex")

    def test_rereading_a_record_corrects_its_source(self):
        record = {"uid": "c:a:r", "ts": 1, "model": "gpt-6-astra", "input": 1, "output": 1}
        self.db.upsert_records([dict(record, source="claude")])
        self.db.upsert_records([dict(record, source="codex")])
        self.assertEqual(self.row("c:a:r")["source"], "codex")


class ProxyAccount(Isolated):
    def test_the_account_in_use_wins_over_the_list(self):
        self.write_accounts()
        with patch("tracker.ripple.active_account", return_value=PLUS):
            self.assertEqual(collectors.proxy_chatgpt_account(), PLUS)

    def test_preferred_login_comes_first_when_clauderipple_is_down(self):
        (self.ripple_home / "chatgpt-accounts.json").write_text(json.dumps({"accounts": [
            {"id": "a", "accountId": BIZ}, {"id": "b", "accountId": PLUS, "preferred": True}]}))
        with patch("tracker.ripple.active_account", return_value=None):
            self.assertEqual(collectors.proxy_chatgpt_account(), PLUS)


class QuotaOwner(Isolated):
    def write_rollout(self, reset):
        day = self.root / "codex" / "sessions" / "2026" / "11" / "05"
        day.mkdir(parents=True)
        lines = [{"type": "session_meta", "payload": {"id": "s1", "creator_account_id": BIZ}},
                 {"type": "turn_context", "payload": {"model": "gpt-6.1-sol"}},
                 {"type": "event_msg", "timestamp": "2026-11-05T06:00:00Z", "payload": {
                     "type": "token_count",
                     "rate_limits": {"limit_id": "codex", "secondary": {
                         "used_percent": 66, "window_minutes": 10080, "resets_at": reset}},
                     "info": {"total_token_usage": {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110}}}}]
        (day / "rollout-s1.jsonl").write_text("\n".join(map(json.dumps, lines)) + "\n")

    def test_session_created_by_one_login_but_answered_by_another(self):
        reset = time.time() + 86400
        self.write_rollout(reset)
        live = [quota(PLUS, reset + 1), quota(BIZ, reset + 300000)]
        with patch("tracker.ripple.chatgpt_snapshots", return_value=live):
            collectors.scan_codex(self.db, {})
        self.assertEqual(self.db.query("SELECT account FROM records")[0]["account"], PLUS)
        stored = self.db.get_kv("codex_limits_by_account")
        self.assertIn(f"{PLUS}:codex", stored)
        self.assertNotIn(f"{BIZ}:codex", stored)

    def test_creator_stands_when_no_live_quota_matches(self):
        self.write_rollout(time.time() + 86400)
        with patch("tracker.ripple.chatgpt_snapshots", return_value=[]):
            collectors.scan_codex(self.db, {})
        self.assertEqual(self.db.query("SELECT account FROM records")[0]["account"], BIZ)


class LiveQuotaCard(Isolated):
    def card(self, stored, live):
        self.db.set_kv("codex_limits_by_account", stored)
        with patch("tracker.ripple.chatgpt_snapshots", return_value=live):
            return {a["account"]: a for a in limits.codex_limits(self.db)["accounts"]}

    def test_logged_snapshot_of_another_quota_does_not_replace_the_live_one(self):
        now = time.time()
        cards = self.card({"biz": quota(BIZ, now + 20000, used=64, ts=now)},
                          [quota(BIZ, now + 340000, used=27, ts=now - 600)])
        self.assertEqual(cards[BIZ]["windows"][0]["used_percent"], 27)

    def test_newer_logged_snapshot_of_the_same_quota_wins(self):
        now = time.time()
        cards = self.card({"biz": quota(BIZ, now + 20000, used=40, ts=now)},
                          [quota(BIZ, now + 20030, used=27, ts=now - 600)])
        self.assertEqual(cards[BIZ]["windows"][0]["used_percent"], 40)

    def test_expired_live_snapshot_yields_to_the_log(self):
        now = time.time()
        cards = self.card({"biz": quota(BIZ, now + 20000, used=40, ts=now - 5000)},
                          [quota(BIZ, now - 100, used=90, ts=now - 9000)])
        self.assertEqual(cards[BIZ]["windows"][0]["used_percent"], 40)


class RouterTimeline(Isolated):
    def write_log(self, *lines):
        logs = self.ripple_home / "logs"
        logs.mkdir(exist_ok=True)
        (logs / "router.log").write_text("\n".join(lines) + "\n")

    @staticmethod
    def stamp(ts):
        return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M:%S")

    def test_timeline_reads_accounts_and_skips_unknown_ones(self):
        self.write_accounts()
        now = int(time.time()) - 3600
        self.write_log(f"{self.stamp(now)} OPENAI POST /v1/responses -> 200 1B 1s codex passthrough account=22222222",
                       f"{self.stamp(now)} OPENAI POST /v1/responses -> 200 1B 1s codex passthrough account=99999999",
                       f"{self.stamp(now)} admin: updated ChatGPT account 11111111")
        self.assertEqual(ripple.account_timeline(), [(now, PLUS)])

    def test_records_take_the_account_nearest_in_time_once(self):
        self.write_accounts()
        now = int(time.time()) - 100000
        self.write_log(f"{self.stamp(now)} OPENAI x codex passthrough account=11111111",
                       f"{self.stamp(now + 3600)} OPENAI x codex passthrough account=22222222")
        self.db.upsert_records([
            {"uid": "c:near-biz:r", "source": "codex", "ts": now + 60, "model": "gpt-6", "account": BIZ},
            {"uid": "c:near-plus:r", "source": "codex", "ts": now + 3000, "model": "gpt-6", "account": BIZ},
            {"uid": "x:s:9", "source": "codex", "ts": now + 3700, "model": "gpt-6", "account": None},
            {"uid": "c:long-ago:r", "source": "codex", "ts": now - 90000, "model": "gpt-6", "account": BIZ},
            {"uid": "c:native:r", "source": "claude", "ts": now, "model": "claude-opus-5-5"}])
        collectors._assign_accounts_from_router(self.db)
        self.assertEqual(self.row("c:near-biz:r")["account"], BIZ)
        self.assertEqual(self.row("c:near-plus:r")["account"], PLUS)
        self.assertEqual(self.row("x:s:9")["account"], PLUS)
        self.assertEqual(self.row("c:long-ago:r")["account"], BIZ)
        self.assertIsNone(self.row("c:native:r")["account"])
        self.db.conn.execute("UPDATE records SET account = 'manual' WHERE uid = 'x:s:9'")
        collectors._assign_accounts_from_router(self.db)
        self.assertEqual(self.row("x:s:9")["account"], "manual")


class Prices(unittest.TestCase):
    def test_gpt_6_1_terra_has_a_price(self):
        self.assertEqual(pricing.cost("gpt-6.1-terra", input=1000000, output=1000000, cache_read=1000000), 6.05)


if __name__ == "__main__":
    unittest.main()
