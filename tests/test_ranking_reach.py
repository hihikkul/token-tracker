"""Getting into the ranking and reaching its server, on any PC and any network."""
import io
import re
import ssl
import threading
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from tracker import ranking, tls

ROOT = Path(__file__).resolve().parents[1]


class DashboardLoadsRankingsTogether(unittest.TestCase):
    def test_forced_sync_that_waited_for_another_sends_nothing(self):
        cfg = {'ranking_groups': [{'code': 'AAAA-BBBB', 'token': 'a', 'nick': 'x'}],
               'ranking_global': {'token': 'g', 'nick': 'y'}}
        entered, release, waiting = threading.Event(), threading.Event(), threading.Event()

        def call(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(5))
            return {'ok': True}

        def second():
            waiting.set()
            return ranking.sync(None, {}, force=True)

        with patch.object(ranking.paths, 'load_config', return_value=cfg), patch.object(ranking.paths, 'save_config'), \
                patch.object(ranking, '_score_body', return_value={'periods': {}}), patch.dict(ranking._last_sync, t=0.0), \
                patch.object(ranking, '_lock', threading.Lock()), patch.object(ranking, '_call', side_effect=call) as remote, \
                ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(ranking.sync, None, {}, True)
            self.assertTrue(entered.wait(5))
            other = pool.submit(second)
            self.assertTrue(waiting.wait(5))
            release.set()
            self.assertTrue(first.result(timeout=5))
            self.assertFalse(other.result(timeout=5))
        self.assertEqual(remote.call_count, 2)


class InviteCode(unittest.TestCase):
    def test_code_or_whole_invite_link(self):
        for text in (' abcd-ef23 ', 'ABCD-EF23',
                     'https://token-tracker-rank.pages.dev/?invite=ABCD-EF23',
                     'https://token-tracker-rank.hihikkul-token-tracker.workers.dev/?invite=abcd-ef23&x=1'):
            self.assertEqual(ranking._invite_code(text), 'ABCD-EF23', text)

    def test_link_without_a_code_stays_invalid(self):
        response = {'code': 'ABCD-EF23', 'name': 'x', 'member_id': 'm', 'token': 't'}
        with patch.object(ranking.paths, 'load_config', return_value={}), patch.object(ranking.paths, 'save_config'), \
                patch.object(ranking, '_call', return_value=response) as call:
            with self.assertRaises(ranking.RankingError):
                ranking.join('https://token-tracker-rank.pages.dev/', 'nick')
            call.assert_not_called()
            ranking.join('https://token-tracker-rank.pages.dev/?invite=ABCD-EF23', 'nick')
        self.assertEqual(call.call_args.args[:2], ('POST', '/v1/groups/ABCD-EF23/join'))


class Answer(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(code, body=b'{}'):
    return urllib.error.HTTPError('https://x.invalid', code, 'x', {}, io.BytesIO(body))


class ServerFallback(unittest.TestCase):
    def run_call(self, first, method='GET', cfg=None):
        answers = [first, Answer(b'{"ok": true}')]
        urls = []

        def urlopen(req, timeout):
            urls.append(req.full_url)
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer

        with patch.object(urllib.request, 'urlopen', urlopen):
            try:
                return ranking._call(method, '/v1/x', {} if method != 'GET' else None, cfg=cfg or {}), urls
            except ranking.RankingError as error:
                return error, urls

    def test_reaches_the_backup_when_the_main_address_fails(self):
        for failure, method in ((urllib.error.URLError(OSError('refused')), 'GET'),
                                (urllib.error.URLError(OSError('refused')), 'POST'),
                                (TimeoutError(), 'GET'), (TimeoutError(), 'PUT'), (http_error(503), 'GET')):
            with self.subTest(failure=failure, method=method):
                got, urls = self.run_call(failure, method)
                self.assertEqual(got, {'ok': True})
                self.assertEqual(urls, [ranking.DEFAULT_SERVER + '/v1/x', ranking.BACKUP_SERVER + '/v1/x'])

    def test_a_join_that_may_have_arrived_is_not_sent_twice(self):
        for failure in (TimeoutError(), http_error(503)):
            with self.subTest(failure=failure):
                got, urls = self.run_call(failure, 'POST')
                self.assertIsInstance(got, ranking.RankingError)
                self.assertEqual(urls, [ranking.DEFAULT_SERVER + '/v1/x'])

    def test_a_real_answer_is_final(self):
        got, urls = self.run_call(http_error(404, b'{"error": "group not found"}'), 'POST')
        self.assertEqual(str(got), 'Invite code not found')
        self.assertEqual(len(urls), 1)

    def test_own_server_has_no_backup(self):
        got, urls = self.run_call(urllib.error.URLError(OSError('refused')),
                                  cfg={'ranking': {'server': 'https://mine.invalid'}})
        self.assertIsInstance(got, ranking.RankingError)
        self.assertEqual(urls, ['https://mine.invalid/v1/x'])

    def test_saved_old_address_moves_to_the_new_one(self):
        self.assertEqual(ranking.server_url({'ranking': {'server': ranking.BACKUP_SERVER}}), ranking.DEFAULT_SERVER)
        self.assertEqual(ranking.server_url({'ranking': {'server': ranking.BACKUP_SERVER + '/'}}), ranking.DEFAULT_SERVER)

    def test_errors_say_what_went_wrong(self):
        cert = urllib.error.URLError(ssl.SSLCertVerificationError('certificate verify failed'))
        self.assertIn('certificate', str(ranking._connection_error(cert)))
        self.assertIn('too long', str(ranking._connection_error(TimeoutError())))
        self.assertIn('too long', str(ranking._connection_error(urllib.error.URLError(TimeoutError()))))
        self.assertIn('Could not connect', str(ranking._connection_error(urllib.error.URLError(OSError('refused')))))


class ServerFiles(unittest.TestCase):
    def test_pages_runs_the_same_code_and_database_as_the_worker(self):
        ids = [re.search(r'database_id = "([^"]+)"', (ROOT / p).read_text(encoding='utf-8')).group(1)
               for p in ('server/wrangler.toml', 'server/pages/wrangler.toml')]
        self.assertEqual(ids[0], ids[1])
        self.assertIn("'../../worker.js'", (ROOT / 'server/pages/dist/_worker.js').read_text(encoding='utf-8'))

    def test_dashboard_field_takes_a_whole_invite_link_and_translates_new_errors(self):
        html = (ROOT / 'web/dashboard.html').read_text(encoding='utf-8')
        self.assertIn("name === 'code' ? 200", html)
        for text in ("This PC could not verify the ranking server's certificate.",
                     'The ranking server took too long to answer. Try again in a moment.'):
            self.assertIn(f'"{text}": "', html)


class BundledRoots(unittest.TestCase):
    def test_bundle_alone_covers_the_authorities_cloudflare_uses(self):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.load_verify_locations(cafile=str(tls.APP_DIR / 'assets' / 'cacert.pem'))
        names = {dict(x[0] for x in c['subject']).get('commonName') for c in ctx.get_ca_certs()}
        for name in ('ISRG Root X1', 'ISRG Root X2', 'GTS Root R1', 'GTS Root R4', 'SSL.com TLS ECC Root CA 2022'):
            self.assertIn(name, names)

    def test_every_urlopen_uses_a_verifying_context_with_the_bundle(self):
        ctx = tls.context()
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)
        with patch.object(urllib.request, 'install_opener') as install, patch.object(tls, 'context', return_value=ctx):
            tls.install()
        https = [h for h in install.call_args.args[0].handlers if isinstance(h, urllib.request.HTTPSHandler)]
        self.assertEqual([h._context for h in https], [ctx])


if __name__ == '__main__':
    unittest.main()
