import ctypes
import os
import subprocess
import tkinter as tk
import tempfile
import unittest
from pathlib import Path

from tracker import icon, paths
from tracker.widget import Win
from tracker.windows_shell import APP_ID, PropVariant, PropertyKey, key, method, window_store, set_shortcut_id, shortcut_store


@unittest.skipUnless(os.name == 'nt', 'Windows shell integration')
class TaskbarPinTests(unittest.TestCase):
    def test_running_window_does_not_force_static_icon(self):
        root = tk.Tk()
        try:
            root.overrideredirect(True)
            root.update_idletasks()
            Win.show_in_taskbar(root)
            root.update()
            Win.set_icon(root, paths.APP_ROOT / 'assets' / 'token-tracker.ico')
            with tempfile.TemporaryDirectory() as temp:
                filename = Path(temp) / 'usage.ico'
                for remaining in (78, 0):
                    icon.write_gauge_ico(filename, (remaining, 'codex'))
                    Win.set_icon(root, filename)
            for _ in range(3):
                self.assertTrue(Win.minimize(root))
                root.update()
                root.deiconify()
                root.update()
            store = window_store(Win.hwnd(root))
            values = {}
            try:
                get = method(store, 5, ctypes.POINTER(PropertyKey), ctypes.POINTER(PropVariant))
                for pid in (2, 3, 4, 5):
                    value = PropVariant()
                    self.assertEqual(get(store, ctypes.byref(key(pid)), ctypes.byref(value)), 0)
                    values[pid] = ctypes.wstring_at(value.value) if value.vt == 31 else None
                    ctypes.windll.ole32.PropVariantClear(ctypes.byref(value))
            finally:
                method(store, 2)(store)
            self.assertEqual(values, {pid: None for pid in (2, 3, 4, 5)})
        finally:
            root.destroy()


    def test_shortcut_has_same_identity_as_process(self):
        with tempfile.TemporaryDirectory() as temp:
            filename = Path(temp) / 'Token Tracker.lnk'
            env = dict(os.environ, TT_TEST_LINK=str(filename),
                       TT_TEST_ICON=str(paths.APP_ROOT / 'assets' / 'token-tracker.ico'))
            subprocess.run(['powershell', '-NoProfile', '-Command',
                            '$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:TT_TEST_LINK); '
                            '$s.TargetPath="pythonw.exe"; $s.IconLocation=$env:TT_TEST_ICON+",0"; $s.Save()'],
                           env=env, check=True, capture_output=True)
            set_shortcut_id(filename)
            ole = ctypes.windll.ole32
            initialized = ole.CoInitialize(None)
            link = store = None
            try:
                link, store = shortcut_store(filename)
                value = PropVariant()
                get = method(store, 5, ctypes.POINTER(PropertyKey), ctypes.POINTER(PropVariant))
                self.assertEqual(get(store, ctypes.byref(key(5)), ctypes.byref(value)), 0)
                self.assertEqual(ctypes.wstring_at(value.value), APP_ID)
                ole.PropVariantClear(ctypes.byref(value))
            finally:
                if store: method(store, 2)(store)
                if link: method(link, 2)(link)
                if initialized >= 0: ole.CoUninitialize()
