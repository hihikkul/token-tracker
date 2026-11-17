"""Desktop regressions with temporary app data, no real app or provider requests."""
import contextlib
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import app
from tracker import paths, sources, updater, widget
from tracker.db import DB


class IsolatedDesktop(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.data = Path(temp.name)
        for target, value in (('data_dir', self.data), ('config_path', self.data / 'config.json'),
                              ('APP_ROOT', self.data)):
            isolated = patch.object(paths, target, return_value=value) if callable(getattr(paths, target)) else \
                patch.object(paths, target, value)
            isolated.start()
            self.addCleanup(isolated.stop)
        quiet = patch.object(updater, 'log')
        self.log = quiet.start()
        self.addCleanup(quiet.stop)
        # the developer's own tracker may be running on 8765; never see it, never open a real browser
        for target, value in (('tracker_answers', False), ):
            isolated = patch.object(app, target, return_value=value)
            isolated.start()
            self.addCleanup(isolated.stop)
        no_browser = patch.object(app.webbrowser, 'open')
        no_browser.start()
        self.addCleanup(no_browser.stop)
        self.old_last_good = paths._last_good
        self.addCleanup(setattr, paths, '_last_good', self.old_last_good)
        for name, value in (('NAMES', dict(sources.BUILTIN)), ('_detected', {})):
            isolated = patch.object(sources, name, value)
            isolated.start()
            self.addCleanup(isolated.stop)

    def bar(self, limits=None):
        bar = widget.Widget.__new__(widget.Widget)
        bar.cfg = {'sources': {}, 'widget': {}}
        bar.show = list(sources.names())
        bar.root = Mock()
        bar.root.winfo_x.return_value = 0
        bar.root.winfo_y.return_value = 0
        bar.root.winfo_screenwidth.return_value = 1920
        bar.root.winfo_reqwidth.return_value = 300
        bar.root.winfo_width.return_value = 300
        bar.root.winfo_height.return_value = 36
        bar.root.winfo_rootx.return_value = 0
        bar.root.winfo_rooty.return_value = 50
        bar.frame, bar.bar = Mock(), Mock()
        bar.bar.winfo_children.return_value = []
        bar.engine = Mock(snapshot={'limits': limits or {}, 'updated': 1})
        bar._noted_sources = set()
        bar._note = Mock()
        bar._ring = Mock(return_value=object())
        bar._bind_drag = Mock()
        bar.live_icon = False
        bar._icon_key = object()
        bar.tip = None
        return bar


class InstanceTests(IsolatedDesktop):
    def test_copy_without_lock_on_configured_port_is_opened_not_duplicated(self):
        # the version running before this update holds no lock, but answers on its port
        with patch.object(app, 'tracker_answers', return_value=True), \
                patch.object(app.webbrowser, 'open') as browser:
            self.assertIsNone(app.claim_instance(8765))
        browser.assert_called_once_with('http://127.0.0.1:8765/')
        again = app.claim_instance(8765)  # the lock was released, not left held
        self.assertIsNotNone(again)
        again.close()

    def test_second_launch_opens_actual_port_not_configured_port(self):
        engine, httpd = Mock(), Mock()
        nested = []

        def second_launch():
            self.assertEqual(json.loads((self.data / 'instance.json').read_text())['port'], 8766)
            with patch.object(sys, 'argv', ['app.py', '--port', '9999']):
                app.main()
            nested.append(True)
            httpd.shutdown.assert_not_called()

        fake_widget = Mock()
        fake_widget.return_value.run.side_effect = second_launch
        with patch.object(sys, 'argv', ['app.py', '--port', '8765']), \
                patch.object(app, 'Engine', return_value=engine) as start, \
                patch.object(app, 'serve', return_value=(httpd, 'http://127.0.0.1:8766/')) as serve, \
                patch.object(widget, 'Widget', fake_widget), \
                patch.object(app.webbrowser, 'open') as browser, \
                patch.object(app, 'port_in_use', side_effect=AssertionError('normal launch must use lock')):
            app.main()
        self.assertEqual(nested, [True])
        start.assert_called_once()
        serve.assert_called_once_with(engine, port=8765, background=True)
        browser.assert_called_once_with('http://127.0.0.1:8766/')
        httpd.shutdown.assert_called_once()
        httpd.server_close.assert_called_once()
        self.assertFalse((self.data / 'instance.json').exists())

    def test_lock_excludes_another_process_and_releases_on_close(self):
        lock = app.InstanceLock()
        self.addCleanup(lock.close)
        self.assertTrue(lock.acquire())
        lock.publish('http://127.0.0.1:8766/')
        script = ('import app; lock = app.InstanceLock(); '
                  'print(lock.acquire()); lock.close()')
        env = dict(os.environ, TOKENTRACKER_HOME=str(self.data), PYTHONDONTWRITEBYTECODE='1')
        root = str(Path(app.__file__).resolve().parent)
        result = subprocess.run([sys.executable, '-B', '-c', script], cwd=root, env=env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'False')
        self.assertEqual(lock.port(), 8766)
        lock.close()
        result = subprocess.run([sys.executable, '-B', '-c', script], cwd=root, env=env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'True')

    def test_wait_port_waits_for_old_lock_and_port_before_starting(self):
        old = app.claim_instance(9876)
        self.addCleanup(old.close)
        old.publish('http://127.0.0.1:9876/')
        with patch.object(app.time, 'sleep', side_effect=lambda _: old.close()) as sleep, \
                patch.object(app, 'port_in_use', side_effect=[True, False]) as occupied, \
                patch.object(app.webbrowser, 'open') as browser:
            replacement = app.claim_instance(9876, wait_port=True)
        self.addCleanup(replacement.close)
        self.assertTrue(replacement.locked)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(occupied.call_count, 2)
        browser.assert_not_called()

    def test_wait_port_timeout_does_not_start_another_instance(self):
        old = app.claim_instance(8765)
        self.addCleanup(old.close)
        old.publish('http://127.0.0.1:8766/')
        with patch.object(app.time, 'monotonic', side_effect=[0, 16]), \
                patch.object(app.webbrowser, 'open') as browser:
            with self.assertRaisesRegex(RuntimeError, 'still starting or closing'):
                app.claim_instance(8766, wait_port=True)
        browser.assert_not_called()
        self.assertEqual(old.port(), 8766)

    def test_second_launch_waits_for_startup_to_publish_actual_port(self):
        old = app.claim_instance(8765)
        self.addCleanup(old.close)
        with patch.object(app.time, 'sleep', side_effect=lambda _: old.publish('http://127.0.0.1:8767/')) as sleep, \
                patch.object(app.webbrowser, 'open') as browser:
            self.assertIsNone(app.claim_instance(8765))
        sleep.assert_called_once_with(0.25)
        browser.assert_called_once_with('http://127.0.0.1:8767/')

    def test_wait_port_timeout_after_lock_acquisition_releases_lock(self):
        with patch.object(app.time, 'monotonic', side_effect=[0, 16]), \
                patch.object(app, 'port_in_use', return_value=True):
            with self.assertRaisesRegex(RuntimeError, 'previous dashboard has not closed'):
                app.claim_instance(8766, wait_port=True)
        lock = app.claim_instance(8765)
        self.addCleanup(lock.close)
        self.assertTrue(lock.locked)

    def test_crashed_process_releases_lock_and_stale_metadata_is_ignored(self):
        script = ('import app, os; lock = app.InstanceLock(); '
                  'assert lock.acquire(); lock.publish("http://127.0.0.1:8766/"); os._exit(0)')
        env = dict(os.environ, TOKENTRACKER_HOME=str(self.data), PYTHONDONTWRITEBYTECODE='1')
        result = subprocess.run([sys.executable, '-B', '-c', script],
                                cwd=str(Path(app.__file__).resolve().parent), env=env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        with patch.object(app.webbrowser, 'open') as browser:
            lock = app.claim_instance(8765)
        self.addCleanup(lock.close)
        self.assertTrue(lock.locked)
        self.assertIsNone(lock.port())
        browser.assert_not_called()

    def test_stale_metadata_is_removed_when_lock_is_acquired(self):
        (self.data / 'instance.json').write_text('{"port": 12345}', encoding='utf-8')
        lock = app.claim_instance(8765)
        self.addCleanup(lock.close)
        self.assertIsNone(lock.port())

    def test_port_probe_handles_an_unrelated_listener(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            self.assertTrue(app.port_in_use(listener.getsockname()[1]))

    def test_startup_failure_releases_instance_lock(self):
        with patch.object(sys, 'argv', ['app.py', '--port', '8765']), \
                patch.object(app, 'Engine', side_effect=RuntimeError('startup failed')):
            with self.assertRaisesRegex(RuntimeError, 'startup failed'):
                app.main()
        lock = app.claim_instance(8765)
        self.addCleanup(lock.close)
        self.assertTrue(lock.locked)


class RestartTests(IsolatedDesktop):
    def test_restart_preserves_effective_port(self):
        with patch.object(updater, '_python', return_value='pythonw.exe'), \
                patch.object(updater, '_root', return_value=self.data), \
                patch.object(updater.subprocess, 'Popen') as popen:
            updater.restart(9876)
        command = popen.call_args.args[0]
        self.assertEqual(command, ['pythonw.exe', str(self.data / 'app.py'),
                                   '--wait-port', '--port', '9876'])
        self.assertTrue(popen.call_args.kwargs['close_fds'])

    def test_failed_spawn_keeps_dashboard_and_widget_alive(self):
        bar = self.bar()
        bar.url = 'http://127.0.0.1:9876/'
        bar.quit = Mock()
        with patch.object(updater, 'restart', side_effect=OSError('spawn failed')) as restart:
            bar._restart()
        restart.assert_called_once_with(9876)
        bar.engine.httpd.shutdown.assert_not_called()
        bar.engine.httpd.server_close.assert_not_called()
        bar.engine.stop.assert_not_called()
        bar.quit.assert_not_called()
        bar._note.assert_called_once_with('Update ready. Restart to apply', 15000)

    def test_successful_spawn_precedes_quit(self):
        bar = self.bar()
        bar.url = 'http://127.0.0.1:8766/'
        order = []
        bar.quit = Mock(side_effect=lambda: order.append('quit'))
        with patch.object(updater, 'restart', side_effect=lambda port: order.append(port)):
            bar._restart()
        self.assertEqual(order, [8766, 'quit'])
        bar.engine.httpd.shutdown.assert_not_called()

    def test_git_checkout_without_git_never_falls_back_to_zip(self):
        with patch.object(updater, 'is_git_checkout', return_value=True), \
                patch.object(updater.shutil, 'which', return_value=None), \
                patch.object(updater, '_git') as git, patch.object(updater, 'apply') as apply:
            with self.assertRaisesRegex(RuntimeError, 'git is not installed; update with git pull'):
                updater.install('2.0.0')
        git.assert_not_called()
        apply.assert_not_called()

    def test_dirty_git_checkout_still_refuses_update(self):
        with patch.object(updater, 'is_git_checkout', return_value=True), \
                patch.object(updater.shutil, 'which', return_value='git'), \
                patch.object(updater, '_git', return_value=' M app.py') as git, \
                patch.object(updater, 'apply') as apply:
            with self.assertRaisesRegex(RuntimeError, 'local file changes'):
                updater.install('2.0.0')
        git.assert_called_once_with('status', '--porcelain', '--untracked-files=no')
        apply.assert_not_called()


class ProviderWidgetTests(IsolatedDesktop):
    def test_detected_provider_uses_snapshot_names_and_stable_colour(self):
        bar = self.bar({'acme': {'windows': [{'label': 'Daily', 'used_percent': 20}]}})
        bar.show.append('acme')
        with patch.object(sources, 'names', return_value={**sources.BUILTIN, 'acme': 'Acme'}), \
                patch.object(widget.tk, 'Frame'), patch.object(widget.tk, 'Canvas'), \
                patch.object(widget.tk, 'Label') as label:
            bar._draw()
        texts = [call.kwargs.get('text') for call in label.call_args_list]
        self.assertIn('Acme', texts)
        self.assertIn('80%', texts)
        colour = widget.source_color('acme')
        for key in ('qwen', 'kimi', 'grok'):
            widget.source_color(key)
        self.assertEqual(widget.source_color('acme'), colour)
        self.assertIn(widget.source_color('acme'), widget.PROVIDER_COLORS)
        self.assertEqual(widget.ring_color('acme', 20), widget.source_color('acme'))
        self.assertEqual(widget.source_color('deepseek'), widget.C['deepseek'])
        self.assertNotIn(widget.source_color('deepseek'), widget.PROVIDER_COLORS)

    def test_provider_colour_is_stable_across_python_hash_seeds(self):
        root = str(Path(app.__file__).resolve().parent)
        for seed in ('1', '42'):
            env = dict(os.environ, TOKENTRACKER_HOME=str(self.data), PYTHONHASHSEED=seed,
                       PYTHONDONTWRITEBYTECODE='1')
            result = subprocess.run([sys.executable, '-B', '-c',
                                     'from tracker.widget import source_color; print(source_color("acme"))'],
                                    cwd=root, env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), widget.source_color('acme'))

    def test_no_quota_or_balance_stays_hidden_in_bar_tooltip_and_icon(self):
        bar = self.bar({'qwen': {'status': 'ok', 'windows': [], 'available': True}})
        bar.show.append('qwen')
        with patch.object(sources, 'names', return_value={**sources.BUILTIN, 'qwen': 'Qwen'}):
            self.assertEqual(list(bar._visible_limits(bar.engine.snapshot['limits'])), [])
            bar._taskbar_status(bar.engine.snapshot['limits'])
        bar.root.title.assert_called_once_with('Token Tracker')
        self.assertIsNone(bar._five_hour({'windows': []}))

    def test_deepseek_balance_renders_compact_line_not_fake_percent(self):
        lim = {'status': 'ok', 'available': True, 'windows': [],
               'balance': [{'currency': 'USD', 'total': 12.3, 'granted': 0, 'topped_up': 12.3}]}
        bar = self.bar({'deepseek': lim})
        with patch.object(widget.tk, 'Frame'), patch.object(widget.tk, 'Label') as label:
            bar._draw()
        self.assertEqual([call.kwargs.get('text') for call in label.call_args_list], ['DeepSeek $12.30'])
        bar._ring.assert_not_called()
        bar.root.title.assert_called_with('Token Tracker · DeepSeek $12.30')
        bar.live_icon = True
        with patch.object(widget.Win, 'ok', False), patch.object(widget.tk, 'PhotoImage'):
            bar._taskbar_status(bar.engine.snapshot['limits'])
        self.assertIsNone(bar._icon_key)

    def test_unavailable_balance_uses_muted_text_and_tooltip_note(self):
        bar = self.bar({'deepseek': {'status': 'auth', 'message': 'Check the DeepSeek API key',
                                   'available': False, 'windows': [],
                                   'balance': [{'currency': 'USD', 'total': 0}]}})
        with patch.object(widget.tk, 'Frame'), patch.object(widget.tk, 'Label') as label:
            bar._draw()
        self.assertEqual(label.call_args.kwargs['text'], 'DeepSeek $0.00')
        self.assertEqual(label.call_args.kwargs['fg'], widget.C['muted'])
        bar.tip = Mock()
        bar.tip.winfo_children.return_value = []
        inner = Mock()
        inner.winfo_reqwidth.return_value = 270
        inner.winfo_reqheight.return_value = 100
        inner.winfo_children.return_value = []
        with patch.object(widget.tk, 'Frame', return_value=inner), \
                patch.object(widget.tk, 'Canvas'), patch.object(widget.tk, 'Scrollbar'), \
                patch.object(widget.tk, 'Label') as label, \
                patch.object(widget.Win, 'work_area', return_value=(0, 0, 1920, 1080)):
            bar._fill_tip()
        texts = [call.kwargs.get('text') for call in label.call_args_list]
        self.assertIn('DeepSeek $0.00', texts)
        self.assertIn('Balance unavailable for use', texts)
        self.assertIn('Check the DeepSeek API key', texts)
        self.assertFalse(any('Claude Code' in text for text in texts))
        self.assertFalse(any('%' in text for text in texts))

    def test_disabled_balance_is_hidden_even_before_provider_detection(self):
        bar = self.bar({'deepseek': {'available': True, 'balance': [{'currency': 'USD', 'total': 12.3}]}})
        bar.cfg['sources']['deepseek'] = False
        self.assertEqual(list(bar._visible_limits(bar.engine.snapshot['limits'])), [])

    def test_balance_formats_currencies_and_rejects_invalid_totals(self):
        self.assertEqual(widget.balance_text({'balance': [
            {'currency': 'USD', 'total': 12.3}, {'currency': 'CNY', 'total': 5},
            {'currency': 'USD', 'total': 'nan'}, {'currency': 'USD', 'total': None}]}), '$12.30 / ¥5.00')

    def test_new_provider_note_is_shown_once_per_key(self):
        bar = self.bar()
        snap = {'new_sources': ['deepseek']}
        bar._notify_sources(snap)
        bar._notify_sources(snap)
        bar._notify_sources({'new_sources': []})
        bar._note.assert_called_once_with('New AI detected: DeepSeek', 10000)
        bar._notify_sources({'new_sources': ['deepseek', 'qwen']})
        bar._note.assert_called_with('New AI detected: Qwen', 10000)

    def test_new_provider_note_is_wired_to_snapshot_draw(self):
        bar = self.bar()
        bar.engine.snapshot['new_sources'] = ['deepseek']
        with patch.object(widget.tk, 'Frame'):
            bar._draw()
            bar._draw()
        bar._note.assert_called_once_with('New AI detected: DeepSeek', 10000)


class CliTests(IsolatedDesktop):
    def scan_and_capture(self, command):
        db = DB(':memory:')
        self.addCleanup(db.conn.close)
        output = io.StringIO()

        def scan(target):
            target.upsert_records([{'uid': 'deepseek-test', 'source': 'deepseek', 'ts': 1,
                                    'model': 'deepseek-chat', 'input': 100}])
            return {'deepseek': 1}

        with patch.object(app, 'DB', return_value=db), patch.object(app, 'scan_all', side_effect=scan), \
                patch.object(app.stats, 'today', return_value={}), \
                patch.object(app, 'all_limits', return_value={}), \
                patch.object(paths, 'claude_project_dirs', return_value=[]), \
                patch.object(paths, 'claude_credentials_file', return_value=None), \
                patch.object(paths, 'codex_session_dirs', return_value=[]), \
                patch.object(paths, 'gemini_root', return_value=self.data / 'gemini'), \
                contextlib.redirect_stdout(output):
            command()
        self.assertIn('DeepSeek: deepseek-chat', output.getvalue())
        self.assertEqual(sources.meta()['deepseek']['models'], ['deepseek-chat'])
        self.assertIn('deepseek', db.get_kv('detected_sources'))

    def test_scan_refreshes_and_prints_detected_providers_and_models(self):
        self.scan_and_capture(app.cmd_scan)

    def test_doctor_refreshes_and_prints_detected_providers_and_models(self):
        self.scan_and_capture(app.cmd_doctor)


if __name__ == '__main__':
    unittest.main()
