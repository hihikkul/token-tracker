import unittest
from tracker.widget import Widget


class CompactTooltip(unittest.TestCase):
    def test_model_list_is_one_row_with_most_restrictive_quota(self):
        windows = [{'label': f'model-{i}', 'used_percent': i, 'resets_at': i + 100} for i in range(40)]
        for source in ('antigravity', 'gemini'):
            rows = Widget._tip_windows(source, windows)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['label'], 'Lowest remaining')
            self.assertEqual(rows[0]['used_percent'], 39)
            self.assertEqual(rows[0]['resets_at'], 139)
        self.assertEqual(windows[-1]['label'], 'model-39')
        self.assertEqual(len(windows), 40)

    def test_weekly_and_five_hour_details_are_preserved(self):
        windows = [{'label':'5h','used_percent':14}, {'label':'Weekly','used_percent':100}]
        self.assertEqual(Widget._tip_windows('claude', windows), windows)
        self.assertEqual(Widget._tip_windows('codex', windows), windows)
        self.assertEqual(Widget._tip_windows('antigravity', []), [])
