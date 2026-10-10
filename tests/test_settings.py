import unittest
from unittest.mock import Mock, patch
from tracker import server


class Settings(unittest.TestCase):
    def handler(self):
        engine = Mock()
        handler = server.make_handler(engine).__new__(server.make_handler(engine))
        handler.path = '/api/settings'
        handler.headers = {}
        handler._send = Mock()
        return handler, engine

    def test_read_excludes_ranking_credentials(self):
        handler, _ = self.handler()
        cfg = {'ranking': {'token': 'private'}, 'widget': {'font': 'Calibri'}}
        with patch.object(server.paths, 'load_config', return_value=cfg):
            handler.do_GET()
        response = handler._send.call_args.args[1]
        self.assertNotIn('ranking', response)
        self.assertEqual(response['widget']['font'], 'Calibri')

    def test_save_whitelist_and_ranges(self):
        handler, engine = self.handler()
        cfg = {'widget': {'opacity': .94}, 'ranking': {'token': 'private'}}
        handler._body = Mock(return_value={
            'widget': {'font': ' Calibri ', 'opacity': .93, 'taskbar': False, 'x': 9},
            'subscription_usd_month': {'claude': 20, 'codex': True, 'gemini': -1},
            'auto_update': False, 'ranking': {'token': 'replaced'}})
        with patch.object(server.paths, 'load_config', return_value=cfg), patch.object(server.paths, 'save_config') as save:
            handler.do_POST()
        self.assertEqual(cfg['widget'], {'font': 'Calibri', 'opacity': .93, 'taskbar': False})
        self.assertEqual(cfg['subscription_usd_month'], {'claude': 20})
        self.assertEqual(cfg['ranking']['token'], 'private')
        self.assertFalse(cfg['auto_update'])
        save.assert_called_once_with(cfg)
        engine.poke.assert_called_once()

    def test_malformed_groups_are_ignored(self):
        handler, _ = self.handler()
        handler._body = Mock(return_value={'widget': 'invalid', 'subscription_usd_month': []})
        with patch.object(server.paths, 'load_config', return_value={}), patch.object(server.paths, 'save_config'):
            handler.do_POST()
        handler._send.assert_called_once_with(200, {'ok': True})
