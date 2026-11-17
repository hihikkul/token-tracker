import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from tracker import export, pricing, sources, stats
from tracker.db import DB


ROOT = Path(__file__).resolve().parents[1]


def utc(text):
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()


class PricingStatsFixes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.patch(pricing, 'data_dir', return_value=self.root)
        self.clear_prices()
        self.addCleanup(self.clear_prices)
        self.db = DB(':memory:')
        self.addCleanup(self.db.conn.close)
        self.registry = dict(sources.BUILTIN, deepseek='DeepSeek', acme='Acme')
        self.patch(sources, 'names', side_effect=lambda: dict(self.registry))
        self.patch(export, '_codex_titles', return_value={})

    def patch(self, obj, name, **kwargs):
        p = patch.object(obj, name, **kwargs)
        self.addCleanup(p.stop)
        return p.start()

    def clear_prices(self):
        pricing.price_for.cache_clear()
        pricing._table.cache_clear()

    def override(self, value):
        (self.root / 'prices.json').write_text(json.dumps(value), encoding='utf-8')
        self.clear_prices()

    def record(self, uid, source='claude', model='claude-sonnet', ts=None, **counts):
        self.db.upsert_records([dict(uid=uid, source=source, model=model,
                                     ts=ts if ts is not None else utc('2026-11-17T10:00:00'),
                                     session='shared-session', project='project', **counts)])

    def test_malformed_override_rows_are_skipped_individually_and_reported(self):
        bad = {
            'missing-output': {'input': 1},
            'missing-input': {'output': 1},
            'negative': {'input': -1, 'output': 1},
            'string': {'input': '1', 'output': 1},
            'boolean': {'input': True, 'output': 1},
            'nan': {'input': float('nan'), 'output': 1},
            'infinite': {'input': 1, 'output': float('inf')},
            'bad-read': {'input': 1, 'output': 2, 'cache_read': -1},
            'bad-write': {'input': 1, 'output': 2, 'cache_write': '3'},
            'bad-peak': {'input': 1, 'output': 2, 'peak': {'input': 4}},
            'peak-list': {'input': 1, 'output': 2, 'peak': []},
            'peak-negative': {'input': 1, 'output': 2, 'peak': {'input': 2, 'output': -1}},
            'peak-cache': {'input': 1, 'output': 2, 'peak': {'input': 2, 'output': 4, 'cache_read': None}},
            'not-object': [],
            '': {'input': 1, 'output': 2},
        }
        self.override(dict(bad, valid={'input': 2, 'output': 3},
                           sonnet={'input': 99},
                           peaked={'input': 1, 'output': 2, 'peak': {'input': 2, 'output': 4}}))
        for model in bad:
            with self.subTest(model=model):
                self.assertIsNone(pricing.cost(model, input=1000000))
        self.assertEqual(pricing.cost('valid', input=1000000, output=1000000), 5)
        self.assertEqual(pricing.cost('peaked', input=1000000, peak=True), 2)
        self.assertEqual(pricing.cost('claude-sonnet', input=1000000), 3)
        problems = pricing.errors()
        self.assertEqual(len(problems), len(bad) + 1)
        self.assertTrue(all(isinstance(e, str) and str(self.root / 'prices.json') in e for e in problems))
        problems.clear()
        self.assertEqual(len(pricing.errors()), len(bad) + 1)

    def test_invalid_price_documents_do_not_discard_bundled_prices(self):
        for text in ('[]', 'null', '{broken', '"text"'):
            with self.subTest(text=text):
                (self.root / 'prices.json').write_text(text, encoding='utf-8')
                self.clear_prices()
                self.assertEqual(pricing.cost('claude-sonnet', input=1000000), 3)
                self.assertEqual(len(pricing.errors()), 1)
        (self.root / 'prices.json').write_bytes(b'\xff')
        self.clear_prices()
        self.assertEqual(pricing.cost('claude-sonnet', input=1000000), 3)
        self.assertEqual(len(pricing.errors()), 1)

    def test_cost_never_raises_for_invalid_model_counts_or_rows(self):
        for model in (None, '', [], {}, 1):
            with self.subTest(model=model):
                self.assertIsNone(pricing.cost(model, input=1))
        for count in (None, 'no', [], {}, float('inf'), float('nan'), 10 ** 1000):
            with self.subTest(count=repr(count)[:30]):
                self.assertIsNone(pricing.cost('claude-sonnet', input=count))
        with patch.object(pricing, 'price_for', return_value={'input': 1}):
            self.assertIsNone(pricing.cost('broken', input=1))
        with patch.object(pricing, 'price_for', side_effect=OSError('unreadable')):
            self.assertIsNone(pricing.cost('broken', input=1))

    def test_all_bundled_patterns_win_their_own_longest_substring_match(self):
        rows = json.loads((ROOT / 'prices.json').read_text(encoding='utf-8'))
        for model, row in rows.items():
            if model.startswith('_'):
                continue
            with self.subTest(model=model):
                self.assertEqual(pricing.price_for(model), row)
                self.assertEqual(pricing.price_for('router/' + model + '-20261117'), row)
        for model, input_price, cache_read in (
                ('gpt-6.1-sol', 2, .1), ('gpt-6-sol', 2, .2), ('gpt-5.6-luna', .2, .02)):
            with self.subTest(model=model):
                self.assertEqual(pricing.cost(model, input=1000000), input_price)
                self.assertEqual(pricing.cost(model, cache_read=1000000), cache_read)

    def test_terra_and_legacy_opus_prices_and_review_remains_unpriced(self):
        self.assertEqual(pricing.price_for('gpt-6.1-terra'),
                         {'input': 1, 'output': 5, 'cache_read': .05, 'cache_write': 1.25})
        self.assertEqual(pricing.price_for('claude-opus-4-0'),
                         {'input': 15, 'output': 75, 'cache_read': 1.5, 'cache_write': 18.75})
        self.assertEqual(pricing.cost('claude-opus-4-0', input=1000000), 15)
        self.assertIsNone(pricing.price_for('codex-auto-review'))
        note = json.loads((ROOT / 'prices.json').read_text(encoding='utf-8'))['_note']
        self.assertIn('third-party', note)
        self.assertIn('Nov 2026', note)

    def test_deepseek_off_peak_peak_alias_and_cache_rates(self):
        for model in ('deepseek-flash', 'deepseek-v4-flash', 'deepseek-v4-flash-vision-exp',
                      'deepseek-chat', 'deepseek-reasoner', 'deepseek-v4-pro'):
            rates = (.66, 1.98, .022) if model == 'deepseek-v4-pro' else (.15, .60, .003)
            for peak, factor in ((None, 1), (False, 1), (True, 2), (1, 2)):
                for key, rate in zip(('input', 'output', 'cache_read'), rates):
                    with self.subTest(model=model, peak=peak, key=key):
                        self.assertAlmostEqual(pricing.cost(model, peak=peak, **{key: 1000000}), rate * factor)
                self.assertEqual(pricing.cost(model, cache_write=1000000, peak=peak), 0)
        self.assertEqual(pricing.cost('claude-sonnet', input=1000000, peak=True), 3)

    def test_peak_sql_utc_weekday_weekend_and_every_hour_boundary(self):
        times = {'00:59:59': 0, '01:00:00': 1, '03:59:59': 1, '04:00:00': 0,
                 '05:59:59': 0, '06:00:00': 1, '09:59:59': 1, '10:00:00': 0}
        with sqlite3.connect(':memory:') as con:
            for day, weekday in (('2026-11-16', True), ('2026-11-20', True),
                                 ('2026-11-21', False), ('2026-11-22', False)):
                for clock, expected in times.items():
                    with self.subTest(day=day, clock=clock):
                        actual = con.execute(f'SELECT {pricing.PEAK_SQL} FROM (SELECT ? AS ts)',
                                             (utc(day + 'T' + clock),)).fetchone()[0]
                        self.assertEqual(actual, expected if weekday else 0)

    def test_dashboard_registry_providers_other_and_antigravity_filters(self):
        now = utc('2026-11-17T12:00:00')
        self.registry['new_provider'] = 'New Provider'
        for i, source in enumerate((*self.registry, 'other')):
            self.record(str(i), source=source, input=i + 1)
        with patch.object(stats.time, 'time', return_value=now):
            default = stats.dashboard(self.db)
            antigravity = stats.dashboard(self.db, sources=['antigravity'])
            selected = stats.dashboard(self.db, sources=['deepseek', 'other', 'new_provider'])
            today = stats.today(self.db)
        self.assertEqual(set(default['sources']), set(self.registry) | {'other'})
        self.assertEqual(set(default['by_source']), set(default['sources']))
        self.assertEqual(set(today) - {'all'}, set(default['sources']))
        self.assertEqual(antigravity['sources'], ['antigravity'])
        self.assertEqual(antigravity['totals']['tokens'], 5)
        self.assertEqual(antigravity['today']['all']['tokens'], 5)
        self.assertEqual(set(selected['by_source']), {'deepseek', 'other', 'new_provider'})
        self.assertTrue(all(set(row) == {'date', 'deepseek', 'other', 'new_provider'}
                            for row in selected['daily']))

    def test_explicit_empty_and_unsupported_filters_return_empty_not_defaults(self):
        self.record('present', input=100)
        with patch.object(stats.time, 'time', return_value=utc('2026-11-17T12:00:00')):
            for selected in ([], (), ['not-registered']):
                with self.subTest(selected=selected):
                    result = stats.dashboard(self.db, sources=selected)
                    self.assertEqual(result['sources'], [])
                    self.assertEqual(result['totals']['tokens'], 0)
                    self.assertEqual(result['by_source'], {})
                    self.assertEqual(result['models'], [])
                    self.assertEqual(result['projects'], [])
                    self.assertEqual(result['sessions'], [])
                    self.assertEqual(result['year'], {})
                    self.assertEqual(result['coverage'], {})
                    self.assertEqual(result['today']['all']['tokens'], 0)
                    self.assertEqual(stats.today(self.db, sources=selected)['all']['tokens'], 0)

    def test_future_records_excluded_everywhere_and_now_captured_once(self):
        now = datetime(2026, 11, 17, 12).timestamp()
        self.record('past', ts=now - 1, input=11)
        self.record('boundary', ts=now, input=13)
        self.record('future-today', ts=now + 1, input=1000)
        self.record('future-day', ts=now + 86400, input=2000)
        with patch.object(stats.time, 'time', return_value=now) as clock:
            result = stats.dashboard(self.db, days=7, sources=['claude'])
            clock.assert_called_once_with()
        self.assertEqual(result['generated_at'], now)
        self.assertEqual(result['totals']['tokens'], 24)
        self.assertEqual(result['totals']['calls'], 2)
        self.assertEqual(sum(row['claude']['tokens'] for row in result['daily']), 24)
        self.assertEqual(result['models'][0]['tokens'], 24)
        self.assertEqual(result['projects'][0]['tokens'], 24)
        self.assertEqual(result['sessions'][0]['tokens'], 24)
        self.assertEqual(sum(map(sum, result['hours'])), 24)
        self.assertEqual(sum(result['year'].values()), 24)
        self.assertEqual(result['today']['all']['tokens'], 24)
        self.assertEqual(result['coverage']['claude']['n'], 2)
        self.assertEqual(result['coverage']['claude']['last'], now)
        self.assertEqual(result['daily'][-1]['date'], datetime.fromtimestamp(now).date().isoformat())
        with patch.object(stats.time, 'time', return_value=now) as clock:
            self.assertEqual(stats.today(self.db)['all']['tokens'], 24)
            clock.assert_called_once_with()

    def test_stats_every_priced_aggregate_splits_peak_and_recombines_models(self):
        now = utc('2026-11-16T12:00:00')
        times = ['2026-11-16T00:30:00', '2026-11-16T01:30:00',
                 '2026-11-16T06:30:00', '2026-11-14T02:00:00']
        for i, ts in enumerate(times):
            self.record(str(i), source='deepseek', model='deepseek-flash', ts=utc(ts),
                        input=1000000, output=1000000, cache_read=1000000, cache_write=1000000)
        with patch.object(stats.time, 'time', return_value=now):
            result = stats.dashboard(self.db, sources=['deepseek'], days=7)
        expected = (.15 + .60 + .003) * 6  # two off-peak plus two double-rate calls
        self.assertAlmostEqual(result['totals']['cost'], expected)
        self.assertAlmostEqual(result['by_source']['deepseek']['cost'], expected)
        self.assertAlmostEqual(sum(r['deepseek']['cost'] for r in result['daily']), expected)
        self.assertEqual(len(result['models']), 1)
        self.assertEqual(result['models'][0]['calls'], 4)
        self.assertEqual(result['models'][0]['tokens'], 16000000)
        for section in ('models', 'projects', 'sessions'):
            self.assertAlmostEqual(result[section][0]['cost'], expected)
        local_today = datetime.fromtimestamp(now).date()
        today_expected = sum((.753 if i in (0, 3) else 1.506) for i, ts in enumerate(times)
                             if datetime.fromtimestamp(utc(ts)).date() == local_today)
        self.assertAlmostEqual(result['today']['all']['cost'], today_expected)

    def test_export_sessions_keep_proxied_tools_separate(self):
        self.record('claude', source='claude', model='claude-sonnet', input=1000000)
        self.record('codex', source='codex', model='gpt-6.1-sol', input=2000000)
        with patch.object(export.time, 'time', return_value=utc('2026-11-17T12:00:00')):
            result = export.build(self.db, limits={})
        sessions = {s['tool']: s for s in result['sessions']}
        self.assertEqual(set(sessions), {'Claude Code', 'Codex'})
        self.assertEqual(sessions['Claude Code']['tokens'], 1000000)
        self.assertEqual(sessions['Codex']['tokens'], 2000000)
        self.assertEqual(sessions['Claude Code']['cost_usd'], 3)
        self.assertEqual(sessions['Codex']['cost_usd'], 4)
        self.assertEqual({s['session'] for s in sessions.values()}, {'shared-session'})

    def test_export_uses_live_registry_names_and_other_in_every_dataset(self):
        self.registry['late_provider'] = 'Late Provider'
        keys = ('deepseek', 'other', 'late_provider')
        for key in keys:
            self.record(key, source=key, model='unknown-' + key, input=10)
        limits = {key: {'windows': [{'label': 'test', 'used_percent': 25}],
                        'status': 'ok'} for key in keys}
        names = {'DeepSeek', 'Other models', 'Late Provider'}
        with patch.object(export.time, 'time', return_value=utc('2026-11-17T12:00:00')):
            result = export.build(self.db, limits=limits)
        for dataset in ('daily', 'daily_detail', 'hours_detail', 'sessions', 'limits', 'limits_raw', 'unpriced'):
            with self.subTest(dataset=dataset):
                self.assertEqual({r['tool'] for r in result[dataset]}, names)
        self.assertEqual(set(result['projects'][0]['tools'].split(', ')), names)

    def test_unpriced_last_90_days_positive_tokens_grouped_and_sorted(self):
        now = utc('2026-11-17T12:00:00')
        self.record('review1', source='codex', model='codex-auto-review', ts=now - 1, input=30)
        self.record('review2', source='codex', model='codex-auto-review', ts=now, output=40)
        self.record('unknown', source='other', model='unlisted', ts=now - 90 * 86400, cache_read=20)
        self.record('old', model='old-unlisted', ts=now - 90 * 86400 - 1, input=10000)
        self.record('future', model='future-unlisted', ts=now + 1, input=20000)
        self.record('priced', model='claude-sonnet', ts=now, input=30000)
        self.record('empty', model='zero-unlisted', ts=now, reasoning=123)
        with patch.object(export.time, 'time', return_value=now):
            result = export.build(self.db, limits={})
        self.assertEqual(result['unpriced'], [
            {'model': 'codex-auto-review', 'tool': 'Codex', 'tokens': 70},
            {'model': 'unlisted', 'tool': 'Other models', 'tokens': 20}])

    def test_export_every_priced_aggregate_uses_peak_sql(self):
        now = utc('2026-11-16T12:00:00')
        for i, ts in enumerate(('2026-11-16T00:30:00', '2026-11-16T01:30:00',
                                '2026-11-16T06:30:00', '2026-11-14T02:00:00')):
            self.record(str(i), source='deepseek', model='deepseek-v4-pro', ts=utc(ts),
                        input=1000000, output=1000000, cache_read=1000000, cache_write=1000000)
        with patch.object(export.time, 'time', return_value=now):
            result = export.build(self.db, limits={})
        expected = (.66 + 1.98 + .022) * 6
        for dataset in ('daily', 'daily_detail', 'projects', 'sessions'):
            with self.subTest(dataset=dataset):
                self.assertAlmostEqual(sum(r['cost_usd'] for r in result[dataset]), expected, places=2)
                self.assertEqual(sum(r['tokens'] for r in result[dataset]), 16000000)
                self.assertEqual(sum(r['calls'] for r in result[dataset]), 4)
        for dataset in ('daily', 'daily_detail'):
            self.assertEqual({r['peak'] for r in result[dataset]}, {0, 1})
        self.assertEqual(len(result['sessions']), 1)
        self.assertIn('peak', result['prices']['deepseek-v4-pro'])


if __name__ == '__main__':
    unittest.main()
