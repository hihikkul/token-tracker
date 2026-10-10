import unittest
from unittest.mock import Mock, patch
from tracker import widget


class AvailableIcon(unittest.TestCase):
    def draw(self, limits):
        bar = widget.Widget.__new__(widget.Widget)
        bar.show = list(widget.NAMES)
        bar.live_icon = True
        bar._icon_key = object()
        bar.root = Mock()
        with patch.object(widget.Win, 'ok', False), patch.object(widget.tk, 'PhotoImage', return_value=object()):
            bar._taskbar_status(limits)
        return bar

    def test_available_ai_replaces_exhausted_weekly_claude(self):
        bar = self.draw({
            'claude': {'windows': [{'label':'5h','used_percent':14}, {'label':'Weekly','used_percent':100}]},
            'codex': {'windows': [{'label':'Weekly','used_percent':25}]},
            'antigravity': {'windows': [{'label':'gemini-pro-agent','used_percent':90}]},
        })
        self.assertEqual(bar._icon_key, (75, 'codex'))
        title = bar.root.title.call_args.args[0]
        self.assertNotIn('Weekly', title)
        self.assertNotIn('gemini-pro-agent', title)

    def test_most_remaining_wins(self):
        bar = self.draw({'codex': {'windows':[{'label':'Weekly','used_percent':25}]},
                         'antigravity': {'windows':[{'label':'gemini-pro-agent','used_percent':0}]}})
        self.assertEqual(bar._icon_key, (100, 'antigravity'))

    def test_all_exhausted_stays_zero(self):
        bar = self.draw({'claude': {'windows':[{'label':'Weekly','used_percent':100}]},
                         'codex': {'windows':[{'label':'Weekly','used_percent':100}]}})
        self.assertEqual(bar._icon_key[0], 0)

    def test_unknown_quota_is_not_available(self):
        bar = self.draw({'gemini': {'status':'no_credentials'}, 'chatgpt': {'status':'unsupported'}})
        self.assertIsNone(bar._icon_key)
