import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from tracker import ranking


class ConcurrentSync(unittest.TestCase):
    def setUp(self):
        self.cfg = {'ranking_groups': [
            {'code': 'AAAA-BBBB', 'token': 'a', 'nick': 'first'},
            {'code': 'CCCC-DDDD', 'token': 'b', 'nick': 'second'}],
            'ranking_global': {'token': 'global', 'nick': 'public'}}
        for mocked in (patch.object(ranking.paths, 'load_config', return_value=self.cfg),
                       patch.object(ranking.paths, 'save_config'),
                       patch.object(ranking, '_score_body', return_value={'periods': {}}),
                       patch.object(ranking, '_last_sync', {'t': 0.0}),
                       patch.object(ranking, '_lock', threading.Lock())):
            mocked.start()
            self.addCleanup(mocked.stop)

    def test_all_puts_overlap_and_first_target_error_wins(self):
        barrier = threading.Barrier(3, timeout=5)
        second_failed = threading.Event()
        first_error = ranking.RankingError('first target')
        finished = []

        def call(method, path, body, token, cfg):
            self.assertEqual(method, 'PUT')
            self.assertIs(cfg, self.cfg)
            self.assertTrue(ranking._lock.locked())
            barrier.wait()  # Sequential PUTs cannot pass this barrier.
            if token == 'a':
                self.assertTrue(second_failed.wait(5))
                finished.append(token)
                raise first_error
            finished.append(token)
            if token == 'b':
                second_failed.set()
                raise ranking.RankingError('second target')
            return {'ok': True}

        with patch.object(ranking, '_call', side_effect=call) as remote:
            with self.assertRaises(ranking.RankingError) as caught:
                ranking.sync(None, {}, force=True)
        self.assertIs(caught.exception, first_error)
        self.assertCountEqual(finished, ['a', 'b', 'global'])
        self.assertCountEqual([(c.args[1], c.args[2]['nick'], c.kwargs['token'])
                               for c in remote.call_args_list], [
            ('/v1/groups/AAAA-BBBB/score', 'first', 'a'),
            ('/v1/groups/CCCC-DDDD/score', 'second', 'b'),
            ('/v1/global/score', 'public', 'global')])
        self.assertGreater(ranking._last_sync['t'], 0)
        self.assertFalse(ranking._lock.locked())

    def test_success_and_recent_sync_skip(self):
        with patch.object(ranking, '_call', return_value={'ok': True}) as remote:
            self.assertTrue(ranking.sync(None, {}, force=True))
            self.assertFalse(ranking.sync(None, {}))
        self.assertEqual(remote.call_count, 3)

    def test_lock_prevents_overlapping_syncs(self):
        entered = threading.Event()
        release = threading.Event()
        second_started = threading.Event()

        def call(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(5))
            return {'ok': True}

        def second_sync():
            second_started.set()
            return ranking.sync(None, {})

        with patch.object(ranking, '_call', side_effect=call) as remote:
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(ranking.sync, None, {})
                try:
                    self.assertTrue(entered.wait(5))
                    second = pool.submit(second_sync)
                    self.assertTrue(second_started.wait(5))
                    self.assertFalse(second.done())
                finally:
                    release.set()
                self.assertTrue(first.result(timeout=5))
                self.assertFalse(second.result(timeout=5))
        self.assertEqual(remote.call_count, 3)

    def test_rankings_keep_120_second_forced_freshness(self):
        for age, force in ((120, False), (121, True)):
            for get in (ranking.ranking, ranking.global_ranking):
                with self.subTest(age=age, endpoint=get.__name__), \
                        patch.object(ranking.time, 'time', return_value=1000), \
                        patch.dict(ranking._last_sync, t=1000 - age), \
                        patch.object(ranking, 'sync', side_effect=ranking.RankingError('offline')) as sync, \
                        patch.object(ranking, '_call', return_value={'members': []}):
                    get(None, {}, 'week')
                    sync.assert_called_once_with(None, {}, force=force)


if __name__ == '__main__':
    unittest.main()
