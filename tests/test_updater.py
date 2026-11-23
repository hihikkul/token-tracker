import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from tracker import updater


def tree(root, files):
    for rel, text in files.items():
        p = Path(root) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding='utf-8')


def read(root):
    return {p.relative_to(root).as_posix(): p.read_text(encoding='utf-8')
            for p in Path(root).rglob('*') if p.is_file()}


OLD = {'app.py': 'old app', 'VERSION': '1.0.0', 'tracker/engine.py': 'old engine'}
NEW = {'app.py': 'new app', 'VERSION': '2.0.0', 'tracker/engine.py': 'new engine', 'tracker/added.py': 'added'}


class SwapIn(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.root, self.new, self.backup = base / 'install', base / 'new', base / 'backup'
        tree(self.root, OLD)
        tree(self.new, NEW)
        quiet = patch.object(updater, 'log')  # keep test failures out of the real update.log
        self.log = quiet.start()
        self.addCleanup(quiet.stop)

    def test_all_files_are_replaced_and_version_goes_last(self):
        order = []
        real = updater._replace_file
        with patch.object(updater, '_replace_file', side_effect=lambda s, d: order.append(d.name) or real(s, d)):
            updater._swap_in(self.new, self.root, self.backup)
        self.assertEqual(read(self.root), NEW)
        self.assertEqual(order[-1], 'VERSION')

    def test_failure_midway_puts_every_file_back(self):
        real = updater._replace_file

        def disk_error_on_added(src, dst):
            if dst.name == 'added.py':
                raise OSError('disk full')
            return real(src, dst)

        with patch.object(updater, '_replace_file', side_effect=disk_error_on_added):
            with self.assertRaises(OSError):
                updater._swap_in(self.new, self.root, self.backup)
        self.assertEqual(read(self.root), OLD)  # VERSION still old, so the update is offered again

    def test_cleanup_preserves_unrelated_old_files(self):
        tree(self.root, {'notes.old': 'keep', 'Other.exe.old': 'keep', 'TokenTracker.exe.old': 'remove'})
        updater.cleanup_launcher(self.root)
        self.assertEqual(read(self.root), dict(OLD, **{'notes.old': 'keep', 'Other.exe.old': 'keep'}))

    def test_running_exe_is_renamed_aside_and_restored_on_failure(self):
        tree(self.root, {'TokenTracker.exe': 'old exe'})
        tree(self.new, {'TokenTracker.exe': 'new exe'})
        exe = self.root / 'TokenTracker.exe'
        real_os_replace = updater.os.replace

        def exe_in_use(src, dst):
            if Path(dst) == exe and Path(src).name.endswith('.part') and not (self.root / 'TokenTracker.exe.old').exists():
                raise PermissionError('in use')
            if Path(dst).name == 'VERSION':
                raise OSError('disk full')
            return real_os_replace(src, dst)

        with patch.object(updater.os, 'replace', side_effect=exe_in_use):
            with self.assertRaises(OSError):
                updater._swap_in(self.new, self.root, self.backup)
        files = read(self.root)
        self.assertEqual(files['TokenTracker.exe'], 'old exe')
        self.assertEqual({k: v for k, v in files.items() if k != 'TokenTracker.exe'}, OLD)


class Apply(unittest.TestCase):
    def test_apply_installs_downloaded_zip(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as zf:
            for rel, text in NEW.items():
                zf.writestr(f'token-tracker-main/{rel}', text)
        with tempfile.TemporaryDirectory() as tmp:
            root, data = Path(tmp) / 'install', Path(tmp) / 'data'
            tree(root, OLD)
            data.mkdir()
            response = io.BytesIO(buf.getvalue())
            with patch.object(updater.urllib.request, 'urlopen', return_value=response), \
                    patch.object(updater, '_root', return_value=root), \
                    patch.object(updater.paths, 'data_dir', return_value=data):
                updater.apply('2.0.0')
            self.assertEqual(read(root), NEW)
            self.assertEqual(list(data.iterdir()), [])  # unpacked files and backups are cleaned up


if __name__ == '__main__':
    unittest.main()
