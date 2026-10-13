"""GPT and other models answering inside Claude Code (e.g. through ClaudeRipple) are not Claude usage."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tracker.collectors import claude_log_source  # noqa: E402


class ProxyModels(unittest.TestCase):
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
            d = db.DB(root / "t.db")
            # a record stored by an older version under Claude
            d.upsert_records([{"uid": "c:old:1", "source": "claude", "ts": 1, "model": "gpt-6.1-sol",
                               "input": 1, "output": 1}])
            collectors.scan_claude(d, {})
            got = {r["model"]: r["source"] for r in d.query("SELECT model, source FROM records")}
            self.assertEqual(got, {"claude-opus-5-5": "claude", "gpt-6-astra@high": "codex",
                                   "deepseek-v4": "other", "gpt-6.1-sol": "codex"})


if __name__ == "__main__":
    unittest.main()
