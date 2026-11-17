"""Balance polling, provider dispatch, and read-only macOS sign-in regressions."""
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tracker import google_limits, limits, paths, sources
from tracker.db import DB


class _Server(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.requests.append((self.path, dict(self.headers)))
        if self.server.offline:
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
            return
        self.send_response(self.server.code)
        if self.server.redirect:
            self.send_header('Location', self.server.redirect)
        self.end_headers()
        self.wfile.write(json.dumps(self.server.body).encode())


class LimitsFixes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(('127.0.0.1', 0), _Server)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = DB(Path(self.tmp.name) / 'limits.db')
        self.addCleanup(self.db.conn.close)
        self.cfg = {'limits_interval_sec': 300}
        patches = [
            patch.dict(os.environ, DEEPSEEK_API_KEY='fake-deepseek-secret',
                       TOKENTRACKER_HOME=self.tmp.name, NO_PROXY='127.0.0.1'),
            patch.object(paths, 'load_config', return_value=self.cfg),
            patch.object(limits, 'DEEPSEEK_URL', self.url + '/user/balance'),
            patch.object(limits, 'CLAUDE_URL', self.url + '/usage'),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.server.requests = []
        self.server.code = 200
        self.server.offline = False
        self.server.redirect = None
        self.server.body = {'is_available': True, 'balance_infos': [
            {'currency': 'USD', 'total_balance': '12.50', 'granted_balance': '2.25',
             'topped_up_balance': '10.25'},
            {'currency': 'CNY', 'total_balance': 20, 'granted_balance': 1.5,
             'topped_up_balance': 18.5},
        ]}

    def test_deepseek_all_limits_exact_shape_and_bearer_header(self):
        # A configured key also works before DeepSeek first appears in the logs.
        with patch.object(sources, 'enabled', return_value=[]), patch.object(limits.time, 'time', return_value=2000):
            result = limits.all_limits(self.db)['deepseek']
        self.assertEqual(result, {
            'source': 'deepseek', 'status': 'ok', 'message': '', 'checked_at': 2000,
            'windows': [], 'available': True,
            'balance': [{'currency': 'USD', 'total': 12.5, 'granted': 2.25, 'topped_up': 10.25},
                        {'currency': 'CNY', 'total': 20.0, 'granted': 1.5, 'topped_up': 18.5}],
        })
        self.assertEqual(len(self.server.requests), 1)
        path, headers = self.server.requests[0]
        self.assertEqual(path, '/user/balance')
        self.assertEqual(headers['Authorization'], 'Bearer fake-deepseek-secret')
        self.assertNotIn('fake-deepseek-secret', json.dumps(result))
        self.assertNotIn('fake-deepseek-secret', json.dumps(self.db.query('SELECT * FROM kv')))

    def test_deepseek_does_not_store_extra_response_fields(self):
        self.server.body['echo'] = 'fake-deepseek-secret'
        result = limits.fetch_deepseek(self.db)
        self.assertNotIn('echo', result)
        self.assertNotIn('fake-deepseek-secret', json.dumps(self.db.query('SELECT * FROM kv')))

    def test_deepseek_unavailable_balance_is_not_an_auth_error(self):
        self.server.body = {'is_available': False, 'balance_infos': []}
        result = limits.fetch_deepseek(self.db)
        self.assertEqual((result['status'], result['available'], result['balance']), ('ok', False, []))

    def test_deepseek_honors_configured_cache_interval_and_force(self):
        self.cfg['limits_interval_sec'] = 900
        with patch.object(limits.time, 'time', return_value=2000):
            first = limits.fetch_deepseek(self.db)
        with patch.object(limits.time, 'time', return_value=2500):
            self.assertEqual(limits.fetch_deepseek(self.db), first)
            self.assertEqual(len(self.server.requests), 1)
            forced = limits.fetch_deepseek(self.db, force=True)
            self.assertEqual(forced['checked_at'], 2500)
        with patch.object(limits.time, 'time', return_value=3401):
            self.assertEqual(limits.fetch_deepseek(self.db)['checked_at'], 3401)
        self.assertEqual(len(self.server.requests), 3)

    def test_deepseek_auth_errors_preserve_cached_balance_and_are_cached(self):
        first = limits.fetch_deepseek(self.db)
        for code in (401, 403):
            with self.subTest(code=code):
                self.server.code = code
                self.server.body = {'error': 'fake-deepseek-secret'}
                result = limits.fetch_deepseek(self.db, force=True)
                self.assertEqual(result['status'], 'auth')
                self.assertEqual(result['balance'], first['balance'])
                count = len(self.server.requests)
                self.assertEqual(limits.fetch_deepseek(self.db), result)
                self.assertEqual(len(self.server.requests), count)
                self.assertNotIn('fake-deepseek-secret', json.dumps(self.db.query('SELECT * FROM kv')))

    def test_deepseek_network_failure_preserves_last_balance(self):
        first = limits.fetch_deepseek(self.db)
        self.server.offline = True
        result = limits.fetch_deepseek(self.db, force=True)
        self.assertEqual(result['status'], 'offline')
        self.assertEqual((result['balance'], result['available']), (first['balance'], True))
        self.assertEqual(self.db.get_kv('deepseek_limits'), result)

    def test_deepseek_first_network_failure_has_empty_balance(self):
        self.server.offline = True
        result = limits.fetch_deepseek(self.db)
        self.assertEqual((result['status'], result['available'], result['balance']), ('offline', False, []))

    def test_deepseek_other_http_errors_are_errors(self):
        for code in (400, 429, 500):
            with self.subTest(code=code):
                self.server.code = code
                result = limits.fetch_deepseek(self.db, force=True)
                self.assertEqual(result['status'], 'error')
                self.assertEqual(result['message'], f'HTTP {code}')

    def test_deepseek_redirect_does_not_forward_the_key(self):
        self.server.code = 302
        self.server.redirect = self.url + '/must-not-receive-key'
        result = limits.fetch_deepseek(self.db)
        self.assertEqual(result['status'], 'error')
        self.assertEqual([path for path, _ in self.server.requests], ['/user/balance'])

    def test_deepseek_invalid_response_is_error_not_partial_balance(self):
        first = limits.fetch_deepseek(self.db)
        for bad in (None, {}, {'is_available': True, 'balance_infos': [{}]},
                    {'is_available': True, 'balance_infos': [
                        {'currency': 'USD', 'total_balance': 'NaN', 'granted_balance': 0,
                         'topped_up_balance': 0}]}):
            with self.subTest(body=bad):
                self.server.body = bad
                result = limits.fetch_deepseek(self.db, force=True)
                self.assertEqual(result['status'], 'error')
                self.assertEqual(result['balance'], first['balance'])

    def test_no_deepseek_entry_without_key_even_with_cached_balance(self):
        limits.fetch_deepseek(self.db)
        with patch.dict(os.environ, DEEPSEEK_API_KEY=''), \
             patch.object(sources, 'enabled', return_value=['deepseek']):
            self.assertEqual(limits.all_limits(self.db), {})
            self.assertIsNone(limits.fetch_deepseek(self.db, force=True))
        self.assertEqual(len(self.server.requests), 1)

    def test_detected_providers_get_no_fake_quota_or_web_placeholder(self):
        with patch.dict(os.environ, DEEPSEEK_API_KEY=''), \
             patch.object(sources, 'enabled', return_value=['qwen', 'kimi', 'acme', 'other', 'deepseek']):
            self.assertEqual(limits.all_limits(self.db), {})
        self.assertEqual(self.server.requests, [])

    def test_existing_web_not_available_entries_are_unchanged(self):
        with patch.dict(os.environ, DEEPSEEK_API_KEY=''), \
             patch.object(sources, 'enabled', return_value=['chatgpt', 'gemini_web']):
            result = limits.all_limits(self.db)
        for source in ('chatgpt', 'gemini_web'):
            self.assertEqual(result[source], {'source': source, 'windows': [], 'status': 'unsupported',
                'message': 'Web token usage and model quotas cannot be fetched automatically. CLI usage is separate.'})

    def test_google_fetch_rejects_unknown_source_even_with_cache(self):
        self.db.set_kv('qwen_limits', {'checked_at': time.time(), 'status': 'ok'})
        with patch.object(google_limits, 'gemini_cli') as gemini, \
             patch.object(google_limits, 'antigravity') as antigravity:
            for force in (False, True):
                with self.subTest(force=force), self.assertRaises(ValueError):
                    google_limits.fetch(self.db, 'qwen', force=force)
            gemini.assert_not_called()
            antigravity.assert_not_called()
        self.assertEqual(self.db.get_kv('qwen_limits')['status'], 'ok')

    def test_google_fetch_dispatches_only_matching_provider(self):
        with patch.object(google_limits, 'gemini_cli', return_value={'status': 'ok'}) as gemini, \
             patch.object(google_limits, 'antigravity', return_value={'status': 'no_data'}) as antigravity:
            self.assertEqual(google_limits.fetch(self.db, 'gemini', force=True)['status'], 'ok')
            antigravity.assert_not_called()
            self.assertEqual(google_limits.fetch(self.db, 'antigravity', force=True)['status'], 'no_data')
            gemini.assert_called_once_with()
            antigravity.assert_called_once_with()

    def _keychain_fetch(self, oauth):
        with patch.object(paths, 'claude_credentials_file', return_value=None), \
             patch.object(paths, 'claude_keychain_oauth', return_value=oauth), \
             patch.object(limits, '_refresh_claude') as refresh, \
             patch.object(limits, '_save_oauth') as save:
            result = limits.fetch_claude(self.db, force=True)
            refresh.assert_not_called()
            save.assert_not_called()
        return result

    def test_expired_keychain_login_asks_to_open_claude_code_without_refresh(self):
        for rt_exp in (time.time() - 60, time.time() + 86400):
            with self.subTest(refresh_expiry=rt_exp):
                result = self._keychain_fetch({'accessToken': 'old', 'refreshToken': 'r1',
                    'expiresAt': (time.time() - 60) * 1000, 'refreshTokenExpiresAt': rt_exp * 1000})
                self.assertEqual(result['status'], 'expired')
                self.assertEqual(result['message'], 'Open Claude Code once to renew the sign-in (macOS keychain)')
                self.assertNotIn('Retrying', result['message'])
        self.assertEqual(self.server.requests, [])

    def test_revoked_keychain_login_asks_to_open_claude_code_without_refresh(self):
        for code in (401, 403):
            with self.subTest(code=code):
                self.server.code = code
                result = self._keychain_fetch({'accessToken': 'old', 'refreshToken': 'r1',
                    'expiresAt': (time.time() + 3600) * 1000})
                self.assertEqual(result['status'], 'relogin')
                self.assertEqual(result['message'], 'Open Claude Code once to renew the sign-in (macOS keychain)')

    def test_valid_keychain_login_can_still_read_usage_without_refresh(self):
        self.server.body = {'five_hour': {'utilization': 12.0, 'resets_at': None}}
        result = self._keychain_fetch({'accessToken': 'old', 'refreshToken': 'r1',
            'expiresAt': (time.time() + 3600) * 1000,
            'refreshTokenExpiresAt': (time.time() + 60) * 1000})
        self.assertEqual((result['status'], result['windows'][0]['used_percent']), ('ok', 12.0))
        self.assertEqual(len(self.server.requests), 1)


if __name__ == '__main__':
    unittest.main()
