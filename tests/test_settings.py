import unittest
from unittest.mock import Mock, patch
from tracker import server


class Settings(unittest.TestCase):
    def handler(self):
        engine = Mock()
        handler = server.make_handler(engine).__new__(server.make_handler(engine))
        handler.path = '/api/settings'
        handler.headers = {'Host': '127.0.0.1:8765'}
        handler.server = Mock(server_address=('127.0.0.1', 8765))
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

    def test_rebound_host_and_foreign_origin_are_refused(self):
        # DNS rebinding: a page on evil.example resolves its own name to 127.0.0.1, so the
        # browser sends Host and Origin for evil.example; both must be refused, reads included.
        for method, headers in (('do_GET', {'Host': 'evil.example:8765'}),
                                ('do_POST', {'Host': 'evil.example:8765', 'Origin': 'http://evil.example:8765'}),
                                ('do_POST', {'Host': '127.0.0.1:8765', 'Origin': 'http://evil.example'}),
                                ('do_GET', {'Host': '127.0.0.1:9999'}),
                                ('do_GET', {})):
            handler, _ = self.handler()
            handler.headers = headers
            handler._body = Mock(return_value={'auto_update': False})
            with patch.object(server.paths, 'load_config', return_value={}) as load, \
                    patch.object(server.paths, 'save_config') as save:
                getattr(handler, method)()
            handler._send.assert_called_once_with(403, {'error': 'forbidden'})
            load.assert_not_called()
            save.assert_not_called()

    def test_loopback_names_are_served(self):
        for host in ('127.0.0.1:8765', 'localhost:8765', 'LOCALHOST:8765', '[::1]:8765'):
            handler, _ = self.handler()
            handler.headers = {'Host': host, 'Origin': f'http://{host.lower()}'}
            handler._body = Mock(return_value={})
            with patch.object(server.paths, 'load_config', return_value={}), patch.object(server.paths, 'save_config'):
                handler.do_POST()
            handler._send.assert_called_once_with(200, {'ok': True})
