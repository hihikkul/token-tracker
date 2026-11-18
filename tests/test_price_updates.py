import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

from tracker import pricing
from tracker.db import DB


REMOTE = 'https://raw.githubusercontent.com/hihikkul/token-tracker/main/prices.json'
LITELLM = 'https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json'
OPENROUTER = 'https://openrouter.ai/api/v1/models'
HOUR = 3600


def lite(input_price, output_price=0.00001, **cache):
    return {'input_cost_per_token': input_price, 'output_cost_per_token': output_price, **cache}


class PriceUpdates(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        env = patch.dict(os.environ, {'TOKENTRACKER_HOME': str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        self.db = DB()
        self.addCleanup(self.db.conn.close)
        self.addCleanup(pricing.reload)
        pricing.reload()
        self.responses = {REMOTE: {}, LITELLM: {}, OPENROUTER: {'data': []}}
        fetch = patch('tracker.pricing._fetch', side_effect=self.fixture)
        self.fetch = fetch.start()
        self.addCleanup(fetch.stop)

    def fixture(self, url, limit):
        response = self.responses[url]
        if isinstance(response, Exception):
            raise response
        return response if isinstance(response, bytes) else json.dumps(response).encode('utf-8')

    def write(self, name, rows):
        (self.root / name).write_text(json.dumps(rows), encoding='utf-8')

    def auto(self):
        return json.loads((self.root / 'prices.auto.json').read_text(encoding='utf-8'))

    def record(self, *models):
        self.db.upsert_records({'uid': str(model), 'model': model, 'ts': 0, 'source': 'codex'}
                               for model in models)

    def test_remote_overrides_bundled_and_user_overrides_remote(self):
        self.record('claude-sonnet')
        self.assertEqual(pricing.price_for('claude-sonnet')['input'], 3)
        self.responses[REMOTE] = {'sonnet': {'input': 9, 'output': 15}, 'bad': {'input': -1}}
        pricing.refresh(self.db, now=0)
        self.fetch.assert_called_once_with(REMOTE, 512 * 1024)
        self.assertEqual(pricing.price_for('claude-sonnet')['input'], 9)
        cached = json.loads((self.root / 'prices.remote.json').read_text(encoding='utf-8'))
        self.assertNotIn('bad', cached)
        self.write('prices.json', {'sonnet': {'input': 7, 'output': 12}})
        pricing.reload()
        self.assertEqual(pricing.price_for('claude-sonnet')['input'], 7)

    def test_invalid_remote_download_keeps_previous_cache(self):
        self.responses[REMOTE] = {'new-remote': {'input': 9, 'output': 15}}
        pricing.refresh(self.db, now=0)
        path = self.root / 'prices.remote.json'
        cached = path.read_bytes()
        with patch.object(pricing, 'reload', wraps=pricing.reload) as reload:
            for i, invalid in enumerate(([], b'{broken'), start=1):
                self.responses[REMOTE] = invalid
                pricing.refresh(self.db, now=i * 24 * HOUR)
                self.assertEqual(path.read_bytes(), cached)
                self.assertEqual(pricing.price_for('new-remote')['input'], 9)
            reload.assert_not_called()
        self.assertEqual(self.fetch.call_count, 3)

    def test_remote_throttled_and_unchanged_document_does_not_reload(self):
        self.responses[REMOTE] = {'new-remote': {'input': 9, 'output': 15}}
        with patch.object(pricing, 'reload', wraps=pricing.reload) as reload:
            pricing.refresh(self.db, now=0)
            pricing.refresh(self.db, now=24 * HOUR - 1)
            self.fetch.assert_called_once_with(REMOTE, 512 * 1024)
            pricing.refresh(self.db, now=24 * HOUR)
            self.assertEqual(self.fetch.call_count, 2)
            reload.assert_called_once_with()

    def test_cached_remote_and_auto_errors_are_reported(self):
        self.write('prices.remote.json', {'sonnet': {'input': -1, 'output': 1}})
        self.write('prices.auto.json', {'_meta': {}, 'bad': {'input': 1}})
        self.assertEqual(pricing.price_for('claude-sonnet')['input'], 3)
        paths = [str(self.root / name) for name in ('prices.remote.json', 'prices.auto.json')]
        problems = pricing.errors()
        self.assertEqual(len(problems), 2)
        for path, problem in zip(paths, problems):
            self.assertIn(path, problem)
        self.fetch.assert_not_called()

    def test_litellm_prefers_exact_then_first_party_in_order(self):
        self.record('EXACT-TRACKER-MODEL', 'first-party-model', 'reseller-only', 'azure/reseller-only')
        self.responses[LITELLM] = {
            'azure/exact-tracker-model': lite(0.000099),
            'openai/exact-tracker-model': lite(0.000005),
            'exact-tracker-model': lite(0.0000020000004, cache_read_input_token_cost=1e-7,
                                        cache_creation_input_token_cost=2.5e-6),
            'azure/first-party-model': lite(0.000099),
            'anthropic/first-party-model': lite(0.000004),
            'openai/first-party-model': lite(0.000003),
            'azure/reseller-only': lite(0.000099),
        }
        self.assertIsNone(pricing.price_for('EXACT-TRACKER-MODEL'))
        pricing.refresh(self.db, now=0)
        self.assertEqual(self.fetch.call_args_list, [call(REMOTE, 512 * 1024),
                          call(LITELLM, 16 * 1024 * 1024), call(OPENROUTER, 8 * 1024 * 1024)])
        auto = self.auto()
        self.assertEqual((auto['_meta']['fetched_at'], auto['_meta']['sources']), (0, ['litellm']))
        self.assertEqual(auto['exact-tracker-model'], {'source': 'litellm', 'input': 2, 'output': 10,
                                                       'cache_read': .1, 'cache_write': 2.5})
        self.assertEqual(pricing.price_for('ExAct-Tracker-Model'), auto['exact-tracker-model'])
        self.assertEqual(pricing.price_for('first-party-model')['input'], 3)
        self.assertIsNone(pricing.price_for('prefix-exact-tracker-model'))
        self.assertIsNone(pricing.price_for('reseller-only'))
        self.assertIsNone(pricing.price_for('azure/reseller-only'))

    def test_openrouter_fallback_when_litellm_rates_are_invalid(self):
        self.record('router-example', 'no-valid-price')
        self.responses[LITELLM] = {'router-example': lite(float('inf')),
                                   'openai/router-example': lite(-1e-6),
                                   'no-valid-price': {'input_cost_per_token': 0.000002}}
        self.responses[OPENROUTER] = {'data': [
            {'id': 'openai/router-example', 'pricing': {'prompt': '0.000002', 'completion': '0.00001',
                                                        'input_cache_read': '0.0000001'}},
            {'id': 'openai/no-valid-price', 'pricing': {'prompt': 'NaN', 'completion': '0'}},
        ]}
        pricing.refresh(self.db, now=0)
        self.assertEqual(self.fetch.call_args.args[0], OPENROUTER)
        self.assertEqual(pricing.price_for('router-example'), {'source': 'openrouter', 'input': 2,
                                                               'output': 10, 'cache_read': .1, 'cache_write': 0})
        self.assertIsNone(pricing.price_for('no-valid-price'))

    def test_openrouter_still_fetches_after_litellm_json_failure(self):
        self.record('router-example')
        self.responses[LITELLM] = b'not JSON'
        self.responses[OPENROUTER] = {'data': [{'id': 'router-example',
                                               'pricing': {'prompt': '0', 'completion': '0.000002'}}]}
        pricing.refresh(self.db, now=0)
        self.assertEqual(self.fetch.call_args.args[0], OPENROUTER)
        self.assertEqual(pricing.cost('router-example', output=1_000_000), 2)

    def test_curated_price_beats_auto_and_refresh_prunes_auto_row(self):
        self.record('fresh-model')
        self.responses[LITELLM] = {'fresh-model': lite(0.000090)}
        pricing.refresh(self.db, now=0)
        self.assertEqual(pricing.price_for('fresh-model')['input'], 90)
        self.write('prices.json', {'fresh': {'input': 7, 'output': 11}})
        pricing.reload()
        self.assertEqual(pricing.price_for('fresh-model')['input'], 7)
        self.assertIn('fresh-model', self.auto())
        self.fetch.reset_mock()
        pricing.refresh(self.db, now=HOUR)
        self.fetch.assert_not_called()
        self.assertNotIn('fresh-model', self.auto())
        self.assertEqual(pricing.price_for('fresh-model')['input'], 7)

    def test_six_hour_throttle_even_for_new_models(self):
        self.record('old-example')
        self.responses[LITELLM] = {'old-example': lite(0.000002), 'new-example': lite(0.000003)}
        pricing.refresh(self.db, now=0)
        self.assertEqual(self.fetch.call_args_list, [call(REMOTE, 512 * 1024), call(LITELLM, 16 * 1024 * 1024)])
        self.fetch.reset_mock()
        self.record('new-example')
        pricing.refresh(self.db, now=6 * HOUR - 1)
        self.fetch.assert_not_called()
        self.assertIsNone(pricing.price_for('new-example'))
        pricing.refresh(self.db, now=6 * HOUR)
        self.fetch.assert_called_once_with(LITELLM, 16 * 1024 * 1024)
        self.assertEqual(pricing.price_for('new-example')['input'], 3)

    def test_model_without_public_price_is_looked_up_weekly(self):
        self.record('private-review')
        self.responses[LITELLM] = {}
        self.responses[OPENROUTER] = {'data': []}
        pricing.refresh(self.db, now=0)
        self.assertIn(call(LITELLM, 16 * 1024 * 1024), self.fetch.call_args_list)
        self.fetch.reset_mock()
        pricing.refresh(self.db, now=6 * 24 * HOUR)  # past the 6-hour throttle, within the week
        self.assertNotIn(call(LITELLM, 16 * 1024 * 1024), self.fetch.call_args_list)
        self.fetch.reset_mock()
        pricing.refresh(self.db, now=7 * 24 * HOUR)
        self.assertIn(call(LITELLM, 16 * 1024 * 1024), self.fetch.call_args_list)
        self.assertIsNone(pricing.price_for('private-review'))

    def test_fetch_failures_keep_old_auto_rows_and_cost(self):
        self.record('old-example')
        self.responses[LITELLM] = {'old-example': lite(0.000002)}
        pricing.refresh(self.db, now=0)
        cached = (self.root / 'prices.auto.json').read_bytes()
        self.responses = {url: OSError('offline') for url in self.responses}
        self.fetch.reset_mock()
        with patch.object(pricing, 'reload', wraps=pricing.reload) as reload:
            pricing.refresh(self.db, now=7 * 24 * HOUR)  # auto-priced models are rechecked weekly
            reload.assert_not_called()
        self.assertEqual(self.fetch.call_count, 3)
        self.assertEqual((self.root / 'prices.auto.json').read_bytes(), cached)
        self.assertEqual(pricing.cost('old-example', input=1_000_000, output=1_000_000), 12)
        self.fetch.reset_mock()
        pricing.refresh(self.db, now=7 * 24 * HOUR + 1)
        self.fetch.assert_not_called()

    def test_reload_makes_curated_and_auto_edits_visible(self):
        self.assertIsNone(pricing.price_for('no-price-yet'))
        self.assertIsNone(pricing.price_for('curated-later'))
        self.write('prices.auto.json', {'no-price-yet': {'input': 12, 'output': 3, 'source': 'litellm'}})
        self.write('prices.json', {'curated-later': {'input': 7, 'output': 11}})
        self.assertIsNone(pricing.price_for('no-price-yet'))
        self.assertIsNone(pricing.price_for('curated-later'))
        pricing.reload()
        self.assertEqual(pricing.cost('no-price-yet', input=1_000_000), 12)
        self.assertEqual(pricing.cost('curated-later', input=1_000_000), 7)
        self.fetch.assert_not_called()
