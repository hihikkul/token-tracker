import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from tracker import paths


class ConfigSafety(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / 'config.json'
        p = patch.object(paths, 'config_path', return_value=self.path)
        p.start(); self.addCleanup(p.stop)
        old = paths._last_good
        paths._last_good = None
        self.addCleanup(lambda: setattr(paths, '_last_good', old))

    def test_parallel_updates_are_all_kept(self):
        paths.save_config(paths.load_config())

        def join(i):
            paths.update_config(lambda cfg: cfg['ranking_groups'].append({'code': f'G{i}', 'token': str(i)}))

        threads = [threading.Thread(target=join, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        codes = {g['code'] for g in paths.load_config()['ranking_groups']}
        self.assertEqual(codes, {f'G{i}' for i in range(20)})

    def test_saving_a_field_keeps_changes_made_since_the_widget_read(self):
        stale = paths.load_config()  # the widget's copy, read a moment ago
        paths.update_config(lambda cfg: cfg['ranking_groups'].append({'code': 'NEW', 'token': 't'}))
        paths.update_config(lambda cfg: cfg['widget'].update(x=10, y=20))  # what _drag_end does now
        cfg = paths.load_config()
        self.assertEqual([g['code'] for g in cfg['ranking_groups']], ['NEW'])
        self.assertEqual((cfg['widget']['x'], cfg['widget']['y']), (10, 20))
        self.assertEqual(stale['ranking_groups'], [])

    def test_damaged_file_falls_back_to_backup_and_is_kept_aside(self):
        paths.update_config(lambda cfg: cfg['ranking_groups'].append({'code': 'KEEP', 'token': 'secret'}))
        self.path.write_text('{"ranking_groups": [', encoding='utf-8')  # half-written hand edit
        cfg = paths.load_config()
        self.assertEqual(cfg['ranking_groups'][0]['token'], 'secret')
        self.assertEqual(self.path.with_name('config.json.broken').read_text(encoding='utf-8'),
                         '{"ranking_groups": [')
        paths.update_config(lambda c: c['widget'].update(x=1))  # the next save repairs config.json
        saved = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual(saved['ranking_groups'][0]['token'], 'secret')

    def test_damaged_file_without_backup_uses_last_good_copy(self):
        paths.update_config(lambda cfg: cfg.update(auto_update=False))
        self.path.with_name('config.json.bak').unlink()
        self.path.write_text('[]', encoding='utf-8')  # valid JSON, but not a config
        self.assertFalse(paths.load_config()['auto_update'])

    def test_write_leaves_no_temp_files(self):
        paths.save_config(paths.load_config())
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()), ['config.json', 'config.json.bak'])


if __name__ == '__main__':
    unittest.main()
