"""Exercise the real Tk/Windows lifecycle; mocks cannot detect wrapper replacement."""
import os
import unittest
from unittest.mock import patch

from tracker import widget


@unittest.skipUnless(os.name == 'nt', 'Windows taskbar lifecycle')
class WindowsMinimize(unittest.TestCase):
    def test_repeated_minimize_restores_same_window_and_icon(self):
        import ctypes
        from ctypes import wintypes

        class Engine:
            snapshot = {'limits': {'claude': {'windows': [
                {'label': '주간', 'used_percent': 100}]}}, 'updated': 1}

        cfg = {'auto_update': False, 'widget': {'taskbar': True},
               'sources': {key: key == 'claude' for key in widget.NAMES}}
        with patch.object(widget.paths, 'load_config', return_value=cfg):
            bar = widget.Widget(Engine(), 'http://127.0.0.1:8765')
        user = ctypes.windll.user32
        user.IsIconic.argtypes = [wintypes.HWND]
        user.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        errors = []
        original = []

        def check_minimized():
            hwnd = widget.Win.hwnd(bar.root)
            self.assertEqual(hwnd, original[0])
            self.assertTrue(user.IsIconic(hwnd))
            self.assertFalse(bar.bar.winfo_ismapped())
            rect = wintypes.RECT()
            user.GetWindowRect(hwnd, ctypes.byref(rect))
            self.assertLess(rect.left, -10000)  # no empty strip on the desktop
            self.assertTrue(user.SendMessageW(hwnd, 0x7f, 1, 0))  # WM_GETICON
            user.ShowWindow(hwnd, 9)  # taskbar restore uses SW_RESTORE

        def check_restored():
            self.assertEqual(widget.Win.hwnd(bar.root), original[0])
            self.assertFalse(user.IsIconic(original[0]))
            self.assertTrue(bar.bar.winfo_ismapped())
            self.assertTrue(bar.root.overrideredirect())
            self.assertTrue(user.SendMessageW(original[0], 0x7f, 1, 0))

        def guard(fn):
            def run():
                try:
                    fn()
                except BaseException as error:
                    errors.append(error)
                    bar.root.quit()
            return run

        bar.root.after(300, guard(lambda: original.append(widget.Win.hwnd(bar.root))))
        for start in (500, 1300, 2100):
            bar.root.after(start, guard(bar.minimize))
            bar.root.after(start + 300, guard(check_minimized))
            bar.root.after(start + 650, guard(check_restored))
        bar.root.after(2900, bar.root.quit)
        try:
            bar.root.mainloop()
        finally:
            bar.root.destroy()
        if errors:
            raise errors[0]
