import tempfile
import unittest
from pathlib import Path

from tracker import sources
from tracker.db import DB


class Registry(unittest.TestCase):
    def setUp(self):
        self.saved = (dict(sources.NAMES), dict(sources._detected))
        self.addCleanup(self.restore)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = DB(Path(tmp.name) / 'usage.db')
        self.addCleanup(self.db.conn.close)

    def restore(self):
        sources.NAMES.clear(); sources.NAMES.update(self.saved[0])
        sources._detected.clear(); sources._detected.update(self.saved[1])

    def test_vendor_names(self):
        cases = {
            'deepseek-v4-pro': 'deepseek', 'deepseek/deepseek-flash': 'deepseek', 'deepseek-ai/DeepSeek-V4': 'deepseek',
            'qwen3-coder-480b': 'qwen', 'kimi-k2': 'kimi', 'z-ai/glm-4.6': 'glm', 'glm-4.6': 'glm',
            'x-ai/grok-4': 'grok', 'mistralai/devstral-medium': 'mistral', 'meta-llama/llama-3.3-70b': 'llama',
            'openai/gpt-oss-120b': 'openai_api', 'gpt-oss:20b': 'openai_api',
            'nousresearch/hermes-4-70b': 'nousresearch', 'openrouter/acme/coder-2': 'acme',
            'hermes-4-70b': 'hermes', 'claude': 'claude_api', '': 'other', 'unknown': 'other', 'x1': 'other',
        }
        for model, key in cases.items():
            self.assertEqual(sources.vendor_source(model), key, model)

    def test_new_provider_is_detected_once_and_listed(self):
        self.db.upsert_records([
            {'uid': 'a', 'source': 'claude', 'ts': 5, 'model': 'claude-opus-4-8'},
            {'uid': 'b', 'source': 'deepseek', 'ts': 10, 'model': 'deepseek-flash'},
            {'uid': 'c', 'source': 'deepseek', 'ts': 20, 'model': 'deepseek-v4-pro'}])
        self.assertEqual(sources.refresh(self.db), ['deepseek'])
        self.assertEqual(sources.refresh(self.db), [])
        self.assertEqual(sources.names()['deepseek'], 'DeepSeek')
        info = sources.meta()['deepseek']
        self.assertEqual((info['kind'], info['detected'], info['first_seen']), ('payg', True, 10))
        self.assertEqual(info['models'], ['deepseek-flash', 'deepseek-v4-pro'])
        self.assertIn('deepseek', sources.enabled({'sources': {'claude': False}}))
        self.assertNotIn('claude', sources.enabled({'sources': {'claude': False}}))
        self.assertTrue(sources.is_subscription('codex'))
        self.assertFalse(sources.is_subscription('deepseek'))

    def test_detected_providers_survive_a_restart(self):
        self.db.upsert_records([{'uid': 'q', 'source': 'qwen', 'ts': 1, 'model': 'qwen3-coder'}])
        sources.refresh(self.db)
        self.restore()  # a fresh process
        self.assertNotIn('qwen', sources.names())
        sources.load(self.db)
        self.assertEqual(sources.names()['qwen'], 'Qwen')


if __name__ == '__main__':
    unittest.main()
