import copy
import io
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from tracker import export, pricing, ranking, server, sources
from tracker.db import DB


class RankingServerFixes(unittest.TestCase):
    def setUp(self):
        self.db = DB(':memory:')
        self.addCleanup(self.db.conn.close)
        self.cfg = {'sources': {key: key == 'claude' for key in sources.names()},
                    'subscription_plans': {'claude': 'pro'},
                    'ranking': {'server': 'https://example.invalid'},
                    'ranking_groups': [dict(code='AAAA-BBBB', token='private', nick='one')],
                    'ranking_global': {}}
        load = patch.object(ranking.paths, 'load_config', side_effect=lambda: copy.deepcopy(self.cfg))
        save = patch.object(ranking.paths, 'save_config', side_effect=self.save_cfg)
        load.start(); save.start()
        self.addCleanup(load.stop); self.addCleanup(save.stop)
        old = ranking._last_sync['t']
        ranking._last_sync['t'] = 0
        self.addCleanup(lambda: ranking._last_sync.update(t=old))

    def save_cfg(self, cfg):
        self.cfg = copy.deepcopy(cfg)
        return True

    def record(self, uid, source='claude', ts=None, model='claude-sonnet', **values):
        self.db.upsert_records([dict(uid=uid, source=source, model=model,
                                    ts=ranking.time.time() if ts is None else ts, **values)])

    def handler(self, path='/api/settings'):
        engine = Mock()
        engine.db = self.db
        engine.snapshot = {'limits': {'codex': {}}, 'updated': 42}
        cls = server.make_handler(engine)
        handler = cls.__new__(cls)
        handler.path = path
        handler.headers = {'Host': '127.0.0.1:8765'}
        handler.server = Mock(server_address=('127.0.0.1', 8765))
        handler._send = Mock()
        return handler, engine

    def test_group_leave_offline_keeps_membership_and_returns_error(self):
        handler, _ = self.handler('/api/ranking/leave')
        handler._body = Mock(return_value={'code': 'AAAA-BBBB'})
        with patch.object(ranking, '_call', side_effect=ranking.RankingError('offline')):
            handler.do_POST()
        self.assertEqual(self.cfg['ranking_groups'][0]['token'], 'private')
        response = handler._send.call_args.args[1]
        self.assertEqual(response['error'], 'offline')
        self.assertNotIn('ok', response)
        self.assertNotIn('private', json.dumps(response))

    def test_group_leave_success_or_bad_token_discards_membership(self):
        member = copy.deepcopy(self.cfg['ranking_groups'])
        for failure in (None, 'bad token', 'Membership expired. Join the group again.'):
            with self.subTest(failure=failure):
                self.cfg['ranking_groups'] = copy.deepcopy(member)
                error = ranking.RankingError(failure) if failure else None
                with patch.object(ranking, '_call', side_effect=error, return_value={'ok': True}) as call:
                    ranking.leave('AAAA-BBBB')
                self.assertEqual(call.call_args.args[:2], ('DELETE', '/v1/groups/AAAA-BBBB/me'))
                self.assertEqual(self.cfg['ranking_groups'], [])

    def test_period_tokens_excludes_disabled_future_and_payg_records(self):
        now = datetime(2026, 11, 17, 12).timestamp()
        with patch.dict(sources.NAMES, {'deepseek': 'DeepSeek', 'other': 'Other models'}), \
                patch.object(ranking.time, 'time', return_value=now), \
                patch.object(ranking, 'windows', return_value=ranking.windows(datetime(2026, 11, 17).date())):
            self.cfg['sources'].update(codex=False, deepseek=True, other=True)
            self.record('enabled', ts=now, input=1, output=2, cache_write=3, cache_read=4)
            self.record('disabled', source='codex', ts=now, input=100)
            self.record('future', ts=now + 1, input=1000)
            self.record('payg', source='deepseek', ts=now, input=10000)
            self.record('other', source='other', ts=now, input=100000)
            self.record('past', ts=datetime(2026, 10, 1).timestamp(), input=1000000)
            self.assertEqual(ranking._period_tokens(self.db, self.cfg), dict.fromkeys(ranking.PERIODS, 10))
            self.cfg['sources']['claude'] = False
            self.assertEqual(ranking._period_tokens(self.db, self.cfg), dict.fromkeys(ranking.PERIODS, 0))

    def test_codex_unknown_usage_never_falls_back_to_source_price(self):
        self.cfg['sources']['codex'] = True
        self.cfg['subscription_plans']['codex'] = 'pro_200'
        self.record('known-paid', input=1000000)
        self.record('unknown-codex', source='codex', model='gpt-5', input=1000000)
        for limits in ({}, {'codex': {'plan': 'Pro 200'}},
                       {'codex': {'accounts': [{'plan': 'Pro 200'}]}}):
            with self.subTest(limits=limits), patch.object(ranking, 'cost', return_value=10):
                period = ranking.report(self.db, limits, self.cfg)['periods']['month']
            self.assertEqual(period['api'], 10)
            self.assertEqual(period['sub'], 20)
            self.assertEqual(period['ratio'], .5)
            self.assertNotIn('codex', period['tools'])
            self.assertEqual(period['tools']['codex:unknown']['api'], 10)
            self.assertIsNone(period['tools']['codex:unknown']['ratio'])
            self.assertIn('codex:unknown', period['excluded'])

    def test_identified_codex_usage_without_snapshot_is_excluded_per_account(self):
        self.cfg['sources']['codex'] = True
        self.record('codex', source='codex', account='unavailable-account', model='gpt-5', input=100)
        with patch.object(ranking, 'cost', return_value=5):
            period = ranking.report(self.db, {}, self.cfg)['periods']['month']
        key = 'codex:' + ranking.account_key('unavailable-account')
        self.assertEqual(period['tools'][key]['api'], 5)
        self.assertIn(key, period['excluded'])
        self.assertEqual(period['api'], 0)

    def test_parallel_ranking_reads_upload_once_but_forced_mutation_uploads(self):
        self.cfg['ranking_groups'] += [dict(code=code, token=code, nick=code) for code in
                                       ('CCCC-DDDD', 'EEEE-FFFF')]
        self.cfg['ranking_global'] = {'token': 'global', 'nick': 'public'}
        barrier = threading.Barrier(4)
        original = ranking.sync

        def simultaneous_sync(*args, **kwargs):
            barrier.wait(timeout=5)
            return original(*args, **kwargs)

        jobs = [(ranking.ranking, (None, {}, 'month', group['code']))
                for group in self.cfg['ranking_groups']]
        jobs.append((ranking.global_ranking, (None, {}, 'month')))
        with patch.object(ranking, '_score_body', return_value={'periods': {}}) as score, \
                patch.object(ranking, '_call', return_value={'members': []}) as call, \
                patch.object(ranking, 'sync', side_effect=simultaneous_sync):
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = [pool.submit(fn, *args) for fn, args in jobs]
                for future in futures:
                    future.result(timeout=10)
            self.assertEqual(sum(c.args[0] == 'PUT' for c in call.call_args_list), 4)
            score.assert_called_once()
            self.assertFalse(original(None, {}, refresh_after=120))
            self.assertTrue(original(None, {}, force=True))
            self.assertEqual(sum(c.args[0] == 'PUT' for c in call.call_args_list), 8)
            self.assertEqual(score.call_count, 2)

    def test_read_refresh_window_is_checked_inside_sync(self):
        with patch.object(ranking, '_score_body', return_value={'periods': {}}), \
                patch.object(ranking, '_call') as call, patch.object(ranking.time, 'time', return_value=1000):
            ranking._last_sync['t'] = 879
            self.assertFalse(ranking.sync(None, {}))
            self.assertTrue(ranking.sync(None, {}, refresh_after=120))
            self.assertFalse(ranking.sync(None, {}, refresh_after=120))
        call.assert_called_once()

    def test_invalid_content_length_returns_json_without_reading_or_mutating(self):
        for path in ('/api/settings', '/api/ranking/join', '/api/refresh', '/api/update'):
            for length, status in (('-1', 400), ('16385', 413), ('invalid', 400)):
                with self.subTest(path=path, length=length):
                    handler, engine = self.handler(path)
                    handler.headers['Content-Length'] = length
                    handler.rfile = Mock()
                    with patch.object(server.paths, 'save_config') as save:
                        handler.do_POST()
                    handler.rfile.read.assert_not_called()
                    save.assert_not_called()
                    engine.poke.assert_not_called()
                    handler._send.assert_called_once_with(status, {'error': 'invalid Content-Length'})
                    self.assertTrue(handler.close_connection)

    def test_body_accepts_length_bounds_and_rejects_invalid_json(self):
        for length in (0, 16384):
            handler, _ = self.handler()
            handler.headers['Content-Length'] = str(length)
            handler.rfile = io.BytesIO(b'{}' + b' ' * (length - 2) if length else b'')
            self.assertEqual(handler._body(), {})
            handler._send.assert_not_called()
        for raw in (b'[1]', b'null', b'{', b'\xff'):
            handler, _ = self.handler()
            handler.headers['Content-Length'] = str(len(raw))
            handler.rfile = io.BytesIO(raw)
            self.assertIsNone(handler._body())
            handler._send.assert_called_once_with(400, {'error': 'invalid JSON body'})

    def test_handler_applies_socket_timeout_and_reports_slow_body(self):
        handler, _ = self.handler()
        handler.request = Mock()
        handler.setup()
        handler.request.settimeout.assert_called_once_with(15)
        handler.headers['Content-Length'] = '1'
        handler.rfile = Mock()
        handler.rfile.read.side_effect = TimeoutError()
        self.assertIsNone(handler._body())
        handler._send.assert_called_once_with(408, {'error': 'request body timed out'})

    def test_settings_lists_dynamic_sources_and_metadata_without_credentials(self):
        handler, _ = self.handler()
        self.cfg['sources'].update(deepseek=False)
        with patch.dict(sources.NAMES, {'deepseek': 'DeepSeek', 'other': 'Other models'}), \
                patch.dict(sources._detected, {'deepseek': {'first_seen': 123, 'models': ['deepseek-v4']}}):
            handler.do_GET()
        data = handler._send.call_args.args[1]
        self.assertFalse(data['sources']['deepseek'])
        self.assertTrue(data['sources']['other'])
        self.assertEqual(data['source_names']['deepseek'], 'DeepSeek')
        self.assertEqual(data['source_meta']['deepseek'], {'name': 'DeepSeek', 'kind': 'payg',
                         'detected': True, 'first_seen': 123, 'models': ['deepseek-v4']})
        self.assertEqual(data['source_meta']['claude']['kind'], 'subscription')
        self.assertNotIn('private', json.dumps(data))

    def test_settings_accepts_only_builtin_monthly_prices_and_registered_selection(self):
        handler, engine = self.handler()
        prices = {key: 7 for key in sources.BUILTIN}
        prices.update(deepseek=100, other=20, imaginary=30)
        handler._body = Mock(return_value={'subscription_usd_month': prices,
                                          'sources': {'deepseek': False, 'other': True, 'imaginary': True}})
        with patch.dict(sources.NAMES, {'deepseek': 'DeepSeek', 'other': 'Other models'}):
            handler.do_POST()
        # only products whose plan price feeds the ratio; a price for ChatGPT/Antigravity would be ignored
        self.assertEqual(self.cfg['subscription_usd_month'], dict.fromkeys(ranking.SOURCES, 7))
        self.assertEqual(self.cfg['sources'], {'deepseek': False, 'other': True})
        engine.poke.assert_called_once()

    def test_data_keeps_all_enabled_registry_rows_and_adds_metadata(self):
        handler, _ = self.handler('/api/data')
        self.cfg['sources'].update(deepseek=True, other=True)
        rows = [{'tool': name} for name in ('Claude Code', 'DeepSeek', 'Other models', 'Codex')]
        data = {key: copy.deepcopy(rows) for key in ('daily', 'daily_detail', 'hours_detail')}
        data['unpriced'] = [{'model': 'unknown', 'tool': 'Other models', 'tokens': 5}]
        with patch.dict(sources.NAMES, {'deepseek': 'DeepSeek', 'other': 'Other models'}), \
                patch.object(export, 'build', return_value=data), \
                patch.object(server.stats, 'coverage', return_value={}):
            handler.do_GET()
        response = handler._send.call_args.args[1]
        for key in ('daily', 'daily_detail', 'hours_detail'):
            self.assertEqual([r['tool'] for r in response[key]], ['Claude Code', 'DeepSeek', 'Other models'])
        self.assertIn('deepseek', response['meta']['source_meta'])
        self.assertEqual(response['meta']['last_scan'], 42)
        self.assertEqual(response['unpriced'], data['unpriced'])

    def test_payg_spend_is_separate_and_never_uploaded(self):
        self.record('subscription', input=1000000)
        self.record('payg', source='deepseek', model='deepseek-v4', input=1000000)
        self.record('other', source='other', model='unknown', input=1000000)
        self.record('disabled', source='qwen', model='qwen', input=1000000)
        with patch.dict(sources.NAMES, {'deepseek': 'DeepSeek', 'other': 'Other models', 'qwen': 'Qwen'}), \
                patch.object(ranking, 'cost', side_effect=lambda model, **_: 10 if model == 'claude-sonnet' else 3):
            self.cfg['sources'].update(deepseek=True, other=True, qwen=False)
            rep = ranking.report(self.db, {}, self.cfg)
            body = ranking._score_body(self.db, {}, self.cfg, 'nick')
            for period, data in rep['periods'].items():
                self.assertEqual(set(data['tools']), {'claude'})
                self.assertEqual(data['api'], 10)
                self.assertEqual(data['payg'], {'deepseek': {'label': 'DeepSeek', 'api': 3},
                                                'other': {'label': 'Other models', 'api': 3}})
                self.assertEqual(data['payg_total'], 6)
                self.assertEqual(body['periods'][period]['usage'], 10)
                self.assertEqual(body['periods'][period]['tokens'], 1000000)
                self.assertNotIn('payg', body['periods'][period])
            self.cfg['sources']['deepseek'] = False
            data = ranking.report(self.db, {}, self.cfg)['periods']['month']
            self.assertNotIn('deepseek', data['payg'])
            self.assertEqual(data['payg_total'], 3)
        self.assertEqual(ranking.SOURCES, ('claude', 'codex', 'gemini'))
        self.assertEqual(set(ranking.PLAN_OPTIONS), set(ranking.SOURCES))

    def test_report_uses_registry_display_names(self):
        self.record('subscription', input=1)
        original = sources.display_name
        with patch.object(sources, 'display_name', side_effect=lambda key: 'New label' if key == 'claude' else original(key)), \
                patch.object(ranking, 'cost', return_value=1):
            rep = ranking.report(self.db, {}, self.cfg)
            body = ranking._score_body(self.db, {}, self.cfg, 'nick')
        self.assertEqual(rep['periods']['month']['tools']['claude']['label'], 'New label')
        self.assertEqual(body['plans'], 'New label Pro')

    def test_peak_groups_are_priced_separately_in_both_aggregates(self):
        peak = datetime(2026, 11, 16, 2, tzinfo=timezone.utc).timestamp()
        offpeak = datetime(2026, 11, 16, 12, tzinfo=timezone.utc).timestamp()
        self.cfg['sources']['codex'] = True
        for source in ('claude', 'codex'):
            self.record(source + '-peak', source=source, ts=peak, account='a', input=1000000)
            self.record(source + '-offpeak', source=source, ts=offpeak, account='a', input=1000000)
        def priced(model, input=0, peak=None, **_):
            return input * (10 if peak else 1) / 1000000
        with patch.object(ranking, 'cost', side_effect=priced) as cost:
            spend = ranking._spend(self.db, peak - 1, offpeak + 1, self.cfg)
            accounts = ranking._codex_spend(self.db, peak - 1, self.cfg)
        self.assertEqual(spend['claude'], 11)
        self.assertEqual(spend['codex'], 11)
        self.assertEqual(accounts, {ranking.account_key('a'): 11})
        self.assertEqual(sorted(c.kwargs['peak'] for c in cost.call_args_list), [0, 0, 0, 1, 1, 1])
        self.assertTrue(pricing.PEAK_SQL)


if __name__ == '__main__':
    unittest.main()
