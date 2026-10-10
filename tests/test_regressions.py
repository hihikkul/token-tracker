import unittest
from datetime import datetime
from unittest.mock import patch

from tracker import ranking, widget, export, google_limits, sources, limits
from tracker.db import DB


class Regressions(unittest.TestCase):
    def test_local_ranking_config_uses_public_service(self):
        cfg = {'ranking': {'server': 'http://localhost:8787'}}
        self.assertEqual(ranking.server_url(cfg), ranking.DEFAULT_SERVER)
        cfg['ranking']['allow_local_server'] = True
        self.assertEqual(ranking.server_url(cfg), 'http://localhost:8787')

    def test_tooltip_uses_above_near_bottom(self):
        x, y, width, height = widget.tooltip_geometry((800, 940, 300, 36), (350, 500), (0, 0, 1920, 1040))
        self.assertEqual(y + height, 932)

    def test_tooltip_uses_below_near_top(self):
        x, y, width, height = widget.tooltip_geometry((800, 10, 300, 36), (350, 500), (0, 0, 1920, 1040))
        self.assertEqual(y, 54)

    def test_oversized_tooltip_stays_in_work_area(self):
        x, y, width, height = widget.tooltip_geometry((800, 940, 300, 36), (350, 1500), (0, 0, 1920, 1040))
        self.assertEqual(y, 0)
        self.assertEqual(height, 932)

    def test_disconnected_apps_are_hidden(self):
        self.assertFalse(sources.connected({'status': 'no_credentials', 'windows': [{'used_percent': 0}]}))
        self.assertFalse(sources.connected({'status': 'unsupported'}))
        self.assertTrue(sources.connected({'status': 'offline', 'windows': [{'used_percent': 10}]}))

    def test_explicit_calibri_is_preserved(self):
        with patch.object(widget.tkfont, 'families', return_value=('Calibri', 'Segoe UI Variable Text')):
            self.assertEqual(widget.pick_fonts(None, 'Calibri', 'Calibri'), ('Calibri', 'Calibri'))

    def test_monthly_subscription_is_full_price(self):
        db = DB(':memory:')
        with patch.object(ranking, 'windows', return_value={
            'month': ('2026-10', datetime(2026, 10, 1).date(), 11)
        }):
            result = ranking.report(db, {'claude': {'plan': 'Pro'}},
                                    {'subscription_usd_month': {}})
        self.assertEqual(result['periods']['month']['sub'], 20)

    def test_weekly_exhaustion_overrides_five_hour(self):
        limit = {'windows': [
            {'label': '5시간', 'used_percent': 14},
            {'label': '주간', 'used_percent': 100},
        ]}
        self.assertEqual(widget.Widget.__new__(widget.Widget)._five_hour(limit)['label'], '주간')

    def test_export_does_not_fetch_limits_when_snapshot_given(self):
        db = DB(':memory:')
        with patch.object(export, 'all_limits', side_effect=AssertionError('network fetch')):
            self.assertEqual(export.build(db, limits={})['daily_detail'], [])

    def test_google_quota_handles_zero_and_missing_amount(self):
        windows = google_limits.quota_windows({'buckets': [
            {'modelId': 'gemini-pro', 'remainingFraction': 0},
            {'modelId': 'gemini-flash', 'remainingFraction': 1},
        ]})
        self.assertEqual([w['used_percent'] for w in windows], [100, 0])

    def test_antigravity_quota_keeps_model_name(self):
        windows = google_limits.quota_windows({'userStatus': {'models': [
            {'label': 'Gemini Pro', 'quotaInfo': {'remainingFraction': .4, 'resetTime': '2026-10-12T00:00:00Z'}}
        ]}})
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0]['label'], 'Gemini Pro')
        self.assertEqual(windows[0]['used_percent'], 60)

    def test_disabled_providers_are_not_called(self):
        with patch.object(sources, 'enabled', return_value=['chatgpt']), \
             patch.object(limits, 'fetch_claude', side_effect=AssertionError('disabled')):
            result = limits.all_limits(DB(':memory:'))
        self.assertEqual(list(result), ['chatgpt'])
        self.assertEqual(result['chatgpt']['status'], 'unsupported')

    def test_future_usage_is_excluded(self):
        db = DB(':memory:')
        db.upsert_records([{'uid':'future','source':'claude','ts':9999999999,
                            'model':'claude-sonnet','input':1000000}])
        self.assertEqual(ranking._spend(db, 0, 2000000000)['claude'], 0)

    def test_google_apps_share_one_subscription(self):
        cfg = {'sources': {key: key in ('gemini', 'antigravity') for key in sources.NAMES},
               'subscription_usd_month': {'gemini': 20}}
        result = ranking.report(DB(':memory:'), {}, cfg)
        self.assertEqual(result['periods']['month']['sub'], 20)

    def test_disabled_usage_is_excluded_with_explicit_config(self):
        db = DB(':memory:')
        db.upsert_records([{'uid': 'test', 'source': 'claude', 'ts': 1,
                            'model': 'claude-sonnet', 'input': 1000000}])
        cfg = {'sources': {key: False for key in sources.NAMES}}
        self.assertEqual(ranking._spend(db, 0, cfg=cfg)['claude'], 0)


if __name__ == '__main__':
    unittest.main()
