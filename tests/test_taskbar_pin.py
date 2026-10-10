import ctypes
import os
import subprocess
import tkinter as tk
import unittest

from tracker import paths
from tracker.shortcuts import _pythonw
from tracker.widget import Win
from tracker.windows_shell import APP_ID, PropVariant, PropertyKey, key, method, window_store


@unittest.skipUnless(os.name == 'nt', 'Windows shell integration')
class TaskbarPinTests(unittest.TestCase):
    def test_pin_uses_tracker_icon_and_relaunches_app(self):
        root = tk.Tk()
        try:
            root.overrideredirect(True)
            root.update_idletasks()
            Win.show_in_taskbar(root)
            root.update()
            Win.set_icon(root, paths.APP_ROOT / 'assets' / 'token-tracker.ico')
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
            self.assertEqual(values[2], subprocess.list2cmdline([str(_pythonw()), str(paths.APP_ROOT / 'app.py')]))
            self.assertEqual(values[3], f'{paths.APP_ROOT / "assets" / "token-tracker.ico"},0')
            self.assertEqual(values[4], 'Token Tracker')
            self.assertEqual(values[5], APP_ID)
        finally:
            root.destroy()
