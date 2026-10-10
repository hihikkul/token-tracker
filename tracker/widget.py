"""Always-on-top limits bar (tkinter, standard library only).

One flat line, about taskbar height:  ◔ Claude 72%  │  ◕ Codex 88%
Each tool shows what is left of its 5-hour window. Hover for every window and its
reset time, double-click for the dashboard, right-click for the menu, drag to move.

On Windows the bar also has a taskbar button whose icon is a gauge with the lowest
5-hour remaining %, so the numbers stay visible while the bar is minimized.
"""
import base64
import os
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import webbrowser

from . import icon, paths, updater
from .sources import NAMES, enabled, connected

C = {
    "bg": "#141416", "edge": "#2e2e33", "ink": "#f4f4f5", "ink2": "#a1a1aa", "muted": "#6b6b73",
    "track": "#2e2e33", "pill": "#26262b", "tip": "#1c1c1f", "menu_hi": "#2b2b30",
    "claude": "#e8794a", "codex": "#5b9cf5", "gemini": "#34c08b",
    "antigravity": "#34c08b", "chatgpt": "#5b9cf5", "gemini_web": "#34c08b",
    "good": "#3fb950", "warning": "#e3b341", "serious": "#f0883e", "critical": "#f85149",
}
SHORT = {**NAMES, 'claude': 'Claude', 'gemini': 'Gemini CLI'}
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
STATUS = {
    "no_credentials": "Not signed in on this PC",
    "expired": "Login expired. Open Claude Code once",
    "no_data": "No data yet",
    "rate_limited": "Rate limited. Showing last values",
    "offline": "Offline. Showing last values",
    "auth": "Sign-in failed. Run /login in Claude Code",
    "error": "Couldn't fetch limits",
    "unsupported": "No limit data available",
}
MAC = sys.platform == "darwin"
RIGHT_CLICK = "<Button-2>" if MAC else "<Button-3>"  # Tk on macOS reports the right button as 2
RELEASES = "https://github.com/hihikkul/token-tracker/releases/latest"
FONT = "Segoe UI"
NUM_FONT = "Segoe UI"
H = 36       # bar height, about a taskbar button
GAUGE = 18   # ring size in the bar


def pick_fonts(root, wanted, wanted_num):
    """User font if installed, else Segoe UI Variable (Windows 11), else Segoe UI, else Tk's default."""
    fams = set(tkfont.families(root))

    def first(*names):
        for n in names:
            if n and n in fams:
                return n
        return tkfont.nametofont("TkDefaultFont").actual("family")
    text = first(wanted, "Segoe UI Variable Text", "Segoe UI",
                 "SF Pro Text", "Helvetica Neue")
    num = first(wanted_num, "Segoe UI Variable Display",
                "Segoe UI Variable Text", "Segoe UI", "SF Pro Display", "SF Pro Text", "Helvetica Neue")
    return text, num


def until(ts):
    s = max(0, int(ts - time.time()))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m = s // 60
    return f"{d}d {h}h" if d else f"{h}h {m}m" if h else f"{max(m, 1)}m"


def reset_in(ts):
    t = time.localtime(ts)
    if ts - time.time() >= 86400:
        return f"Resets in {until(ts)} · {DAYS[t.tm_wday]} {t.tm_mon}/{t.tm_mday}"
    return f"Resets in {until(ts)}"


def clock(ts):
    t = time.localtime(ts)
    if time.strftime("%Y%m%d", t) == time.strftime("%Y%m%d"):
        return time.strftime("%H:%M", t)
    return f"{DAYS[t.tm_wday]} {t.tm_mon}/{t.tm_mday} {time.strftime('%H:%M', t)}"


def wlabel(label):
    """'5시간' -> '5h', '주간' -> 'Weekly', '주간 Opus' -> 'Weekly Opus', '주간 앱' -> 'Weekly Apps'."""
    return (label.replace("5시간", "5h").replace("주간", "Weekly").replace("앱", "Apps")
            .replace("한도", "Limit").replace("시간", "h").replace("일", "d"))


def is_5h(w):
    return w.get("label") == "5시간" or w.get("key") == "five_hour" or w.get("window_minutes") == 300


def sev(used):
    return "critical" if used >= 90 else "serious" if used >= 75 else "warning" if used >= 50 else "good"


def ring_color(src, used):
    """Healthy windows wear the tool's color; from half used on, the warning scale takes over."""
    return C[src] if used < 50 else C[sev(used)]


def num_color(used):
    return C[sev(used)] if used >= 75 else C["ink"]


def rgb(hex_color):
    h = hex_color.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def tooltip_geometry(anchor, size, bounds):
    """Prefer above; fit below when needed, or use the larger side with scrolling."""
    rx, ry, rw, rh = anchor
    tw, th = size
    left, top, right, bottom = bounds
    gap = 8
    above, below = max(0, ry - top - gap), max(0, bottom - ry - rh - gap)
    up = th <= above or (th > below and above >= below)
    height = min(th, max(1, above if up else below))
    width = min(tw, right - left)
    x = max(left, min(rx + rw - width, right - width))
    y = ry - height - gap if up else ry + rh + gap
    return x, max(top, min(y, bottom - height)), width, height


# ---------------------------------------------------------------- Windows chrome
class Win:
    """Small ctypes helpers; every call is a no-op off Windows or if the API is missing."""
    ok = os.name == "nt"

    @staticmethod
    def hwnd(root):
        import ctypes
        from ctypes import wintypes
        get_parent = ctypes.windll.user32.GetParent
        get_parent.argtypes = [wintypes.HWND]
        get_parent.restype = wintypes.HWND
        return get_parent(root.winfo_id())

    @classmethod
    def app_id(cls, name="hihikkul.token-tracker"):
        """Own taskbar identity, so the button shows our icon instead of Python's."""
        if not cls.ok:
            return
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(name)
        except (AttributeError, OSError):
            pass

    @classmethod
    def set_icon(cls, root, filename):
        if not cls.ok:
            return
        import ctypes
        from ctypes import wintypes
        user = ctypes.windll.user32
        user.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR,
                                   wintypes.UINT, ctypes.c_int, ctypes.c_int, wintypes.UINT]
        user.LoadImageW.restype = wintypes.HANDLE
        user.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user.SendMessageW.restype = wintypes.LPARAM
        user.GetParent.argtypes = [wintypes.HWND]
        user.GetParent.restype = wintypes.HWND
        user.DestroyIcon.argtypes = [wintypes.HANDLE]
        user.DestroyIcon.restype = wintypes.BOOL
        handles = [user.LoadImageW(None, str(filename), 1, size, size, 0x10) for size in (16, 32)]
        if not all(handles):
            for handle in handles:
                if handle:
                    user.DestroyIcon(handle)
            return
        for hwnd in (root.winfo_id(), user.GetParent(root.winfo_id())):
            if hwnd:
                for kind, handle in enumerate(handles):
                    user.SendMessageW(hwnd, 0x80, kind, handle)  # WM_SETICON small / large
        old = getattr(root, '_native_icons', [])
        root._native_icons = handles
        for handle in old:
            user.DestroyIcon(handle)

    @classmethod
    def dpi_aware(cls):
        """Crisp text on scaled displays instead of a blurry bitmap stretch."""
        if not cls.ok:
            return
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            try:
                import ctypes
                ctypes.windll.user32.SetProcessDPIAware()
            except (AttributeError, OSError):
                pass

    @classmethod
    def show_in_taskbar(cls, root):
        """A borderless (overrideredirect) window is hidden from the taskbar; put it back."""
        if not cls.ok:
            return
        try:
            import ctypes
            u = ctypes.windll.user32
            h = cls.hwnd(root)
            GWL_EXSTYLE, GWL_STYLE = -20, -16
            ex = u.GetWindowLongW(h, GWL_EXSTYLE)
            u.SetWindowLongW(h, GWL_EXSTYLE, (ex & ~0x00000080) | 0x00040000)  # -TOOLWINDOW +APPWINDOW
            st = u.GetWindowLongW(h, GWL_STYLE)
            u.SetWindowLongW(h, GWL_STYLE, st | 0x00020000)  # MINIMIZEBOX: taskbar click minimizes
            from .shortcuts import _pythonw
            from .windows_shell import configure_pin
            configure_pin(h, _pythonw(), paths.APP_ROOT / 'app.py',
                          paths.APP_ROOT / 'assets' / 'token-tracker.ico')
            root.withdraw()
            root.after(10, root.deiconify)
        except (AttributeError, OSError):
            pass

    @classmethod
    def work_area(cls, root):
        if cls.ok:
            import ctypes
            from ctypes import wintypes
            class MonitorInfo(ctypes.Structure):
                _fields_ = [('size', wintypes.DWORD), ('monitor', wintypes.RECT),
                            ('work', wintypes.RECT), ('flags', wintypes.DWORD)]
            user = ctypes.windll.user32
            user.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
            user.MonitorFromWindow.restype = wintypes.HANDLE
            user.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
            info = MonitorInfo()
            info.size = ctypes.sizeof(info)
            if user.GetMonitorInfoW(user.MonitorFromWindow(cls.hwnd(root), 2), ctypes.byref(info)):
                r = info.work
                return r.left, r.top, r.right, r.bottom
        return (0, 0, root.winfo_screenwidth(), root.winfo_screenheight())

    @classmethod
    def minimize(cls, root):
        """Minimize the existing wrapper without recreating a borderless Tk window."""
        if not cls.ok:
            return False
        import ctypes
        from ctypes import wintypes
        show = ctypes.windll.user32.ShowWindow
        show.argtypes = [wintypes.HWND, ctypes.c_int]
        show.restype = wintypes.BOOL
        show(cls.hwnd(root), 6)  # SW_MINIMIZE; preserve APPWINDOW and the native icon
        return True

    @classmethod
    def round_corners(cls, win, border=None, small=False):
        """Windows 11 rounded corners and border color (ignored on Windows 10)."""
        if not cls.ok:
            return
        try:
            import ctypes
            h = cls.hwnd(win)
            dwm = ctypes.windll.dwmapi
            pref = ctypes.c_int(3 if small else 2)  # DWMWCP_ROUNDSMALL / DWMWCP_ROUND
            dwm.DwmSetWindowAttribute(h, 33, ctypes.byref(pref), ctypes.sizeof(pref))
            if border:
                r, g, b = rgb(border)
                col = ctypes.c_uint(r | (g << 8) | (b << 16))
                dwm.DwmSetWindowAttribute(h, 34, ctypes.byref(col), ctypes.sizeof(col))
        except (AttributeError, OSError):
            pass


class Widget:
    def __init__(self, engine, dashboard_url):
        self.engine = engine
        self.url = dashboard_url
        self.cfg = paths.load_config()
        wc = self.cfg.setdefault("widget", {})
        self.show = enabled(self.cfg)
        self._source_selection = tuple(self.show)
        self.taskbar = bool(wc.get("taskbar", True)) and Win.ok       # Windows taskbar button
        self.live_icon = bool(wc.get("taskbar", True)) and (Win.ok or MAC)  # gauge on the taskbar / Dock icon
        Win.dpi_aware()
        if self.taskbar:
            Win.app_id()
        self.root = tk.Tk()
        global FONT, NUM_FONT
        FONT, NUM_FONT = pick_fonts(self.root, wc.get("font"), wc.get("number_font"))
        self.root.title("Token Tracker")
        self.engine.update_callback = lambda: self.root.after(0, lambda: self._update(manual=True))
        try:
            self.root.iconbitmap(default=str(paths.APP_ROOT / "assets" / "token-tracker.ico"))
        except (tk.TclError, AttributeError):
            pass
        self.root.overrideredirect(True)
        self.root.configure(bg=C["bg"])
        self.root.attributes("-topmost", bool(wc.get("topmost", True)))
        try:
            self.root.attributes("-alpha", float(wc.get("opacity", 0.96)))
        except tk.TclError:
            pass

        # 1px edge drawn by the frame on Windows 10; Windows 11 adds its own rounded border
        self.frame = tk.Frame(self.root, bg=C["bg"], highlightthickness=1, highlightbackground=C["edge"])
        self.frame.pack(fill="both", expand=True)
        self.bar = tk.Frame(self.frame, bg=C["bg"], height=H - 2)
        self.bar.pack(fill="both", expand=True, padx=12)
        self.status = tk.Label(self.frame, text="", bg=C["bg"], fg=C["muted"], font=(FONT, 9))
        self._rings = {}
        self._last_draw = 0
        self._last_min = None
        self._icon_key = None
        self._icons = []
        self.tip = None
        self._tip_job = None
        self._build_menu()
        for w in (self.root,):
            w.bind(RIGHT_CLICK, self._popup)
        self._draw()
        self._place(wc)
        Win.round_corners(self.root, C["edge"])
        if self.taskbar:
            Win.show_in_taskbar(self.root)
            self.root.after(100, self._restore_icon)
            self.root.bind("<Map>", self._on_map)
        self.root.bind("<Enter>", self._hover_in)
        self.root.bind("<Leave>", self._hover_out)
        self.root.after(300, self._tick)
        if self.cfg.get("auto_update", True):
            self.root.after(20_000, self._auto_update)

    def _place(self, wc):
        self.root.update_idletasks()
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        w = self.root.winfo_reqwidth()
        x = wc.get("x") if wc.get("x") is not None else sw - w - 24
        y = wc.get("y") if wc.get("y") is not None else sh - 64 - H
        x = max(0, min(int(x), max(0, sw - w)))
        y = max(0, min(int(y), sh - 20))
        self.root.geometry(f"+{x}+{y}")

    # ---------------------------------------------------------------- menu
    def _build_menu(self):
        m = tk.Menu(self.root, tearoff=0, bg=C["tip"], fg=C["ink"], activebackground=C["menu_hi"],
                    activeforeground=C["ink"], disabledforeground=C["muted"], bd=0, relief="flat",
                    font=(FONT, 9))
        m.add_command(label="Open Dashboard", command=self.open_dashboard)
        m.add_command(label="Refresh Now", command=self.refresh)
        m.add_command(label="Check for Updates", command=lambda: self._update(manual=True))
        if self.taskbar:
            m.add_command(label="Minimize to Taskbar", command=self.minimize)
        self._top = tk.BooleanVar(value=bool(self.root.attributes("-topmost")))
        m.add_checkbutton(label="Always on Top", variable=self._top, command=self.toggle_topmost,
                          selectcolor=C["ink"])
        m.add_separator()
        m.add_command(label="Settings", command=lambda: webbrowser.open(self.url.rstrip('/') + '/#v=settings'))
        m.add_command(label="Quit", command=self.quit)
        m.add_separator()
        m.add_command(label=f"token-tracker v{updater.local_version()}", state="disabled")
        self.menu = m

    def _popup(self, e):
        self._hide_tip()
        self.menu.tk_popup(e.x_root, e.y_root)

    def _bind_drag(self, w):
        w.bind("<ButtonPress-1>", self._drag_start)
        w.bind("<B1-Motion>", self._drag)
        w.bind("<ButtonRelease-1>", self._drag_end)
        w.bind("<Double-Button-1>", lambda e: self.open_dashboard())
        w.bind(RIGHT_CLICK, self._popup)
        w.configure(cursor="fleur")

    def _drag_start(self, e):
        self._hide_tip()
        self._dx, self._dy = e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y()

    def _drag(self, e):
        self.root.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")

    def _drag_end(self, e):
        self.cfg["widget"]["x"], self.cfg["widget"]["y"] = self.root.winfo_x(), self.root.winfo_y()
        paths.save_config(self.cfg)

    # --------------------------------------------------------------- actions
    def open_dashboard(self):
        webbrowser.open(self.url)

    def refresh(self):
        self._note("Refreshing…", 0)

        def work():
            self.engine.refresh(force_limits=True)
            self.root.after(0, lambda: self._note("", 0))
        threading.Thread(target=work, daemon=True).start()

    def minimize(self):
        """Keep the Windows taskbar wrapper intact while minimizing."""
        self._hide_tip()
        if Win.minimize(self.root):
            return
        self.root.overrideredirect(False)
        self.root.iconify()

    def _on_map(self, e):
        if e.widget is self.root:
            self.root.after(100, self._restore_icon)
        if e.widget is self.root and not self.root.overrideredirect() and self.root.state() == "normal":
            self.root.overrideredirect(True)
            self.root.after(10, lambda: (Win.show_in_taskbar(self.root), Win.round_corners(self.root, C["edge"])))

    def _restore_icon(self):
        self._icon_key = object()
        self._taskbar_status(self.engine.snapshot.get('limits') or {})

    def toggle_topmost(self):
        v = bool(self._top.get())
        self.root.attributes("-topmost", v)
        self.cfg["widget"]["topmost"] = v
        paths.save_config(self.cfg)

    def _note(self, text, ms=6000):
        """Short status text at the end of the bar ('Refreshing…', update results)."""
        if text:
            self.status.configure(text=text)
            self.status.pack(side="right", padx=(0, 12), before=self.bar) if not self.status.winfo_ismapped() else None
        else:
            self.status.pack_forget()
        if ms:
            self.root.after(ms, lambda: self._note("", 0) if self.status.cget("text") == text else None)

    # ---------------------------------------------------------------- update
    def _auto_update(self):
        self._update(manual=False)
        hours = float(self.cfg.get("update_check_hours", 1))
        self.root.after(int(hours * 3600 * 1000), self._auto_update)

    def _update(self, manual):
        if getattr(self, '_updating', False):
            return
        if updater.is_frozen():
            self._updating = True
            if manual:
                self._note('Checking…', 0)
            def check():
                try:
                    new = updater.check()
                except Exception:
                    self._updating = False
                    if manual:
                        self.root.after(0, lambda: self._note('Update check failed', 10000))
                    return
                self._updating = False
                if not new and manual:
                    self.root.after(0, lambda: self._note(f'Up to date (v{updater.local_version()})'))
                if new and not getattr(self, "_offered", None):
                    self._offered = new
                    self.root.after(0, lambda: (
                        self.menu.insert_command(0, label=f"Download v{new}", command=lambda: webbrowser.open(RELEASES)),
                        self._note(f"v{new} available · right-click to download", 20000)))
            threading.Thread(target=check, daemon=True).start()
            return
        if updater.is_git_checkout():
            if manual:
                self._note("Git checkout: update with git pull")
            return
        if manual:
            self._note("Checking…", 0)
        self._updating = True

        def work():
            try:
                new = updater.check()
                if not new:
                    if manual:
                        self.root.after(0, lambda: self._note(f"Up to date (v{updater.local_version()})"))
                    return
                self.root.after(0, lambda: self._note(f"Updating to v{new}…", 0))
                updater.apply(new)
                self.root.after(0, self._restart)
            except Exception as e:  # network down, GitHub busy, files locked…
                if manual:
                    msg = str(e) or e.__class__.__name__
                    self.root.after(0, lambda: self._note(f"Update failed: {msg[:40]}", 10000))
            finally:
                self._updating = False
        threading.Thread(target=work, daemon=True).start()

    def _restart(self):
        try:
            updater.restart()
        except OSError:
            self._note("Update ready. Restart to apply", 15000)
            return
        self.quit()

    def quit(self):
        self.engine.stop()
        self.root.destroy()

    # ------------------------------------------------------------------ data
    @staticmethod
    def _accounts(lim):
        """[(plan, windows, fetched_at)] for one tool, most recently used account first."""
        if lim.get("accounts"):
            return [(a.get("plan"), a["windows"], a.get("fetched_at")) for a in lim["accounts"]]
        if lim.get("windows"):
            return [(lim.get("plan"), lim["windows"], lim.get("fetched_at"))]
        return []

    def _five_hour(self, lim):
        """The 5-hour window of the most recently used account (else its first window)."""
        accts = self._accounts(lim)
        if not accts:
            return None
        wins = accts[0][1]
        # The tightest applicable quota limits use, even if 5h is still available.
        return max(wins, key=lambda w: float(w.get('used_percent') or 0)) if wins else None

    @staticmethod
    def _tip_windows(src, wins):
        if src not in ('antigravity', 'gemini') or not wins:
            return wins
        tightest = max(wins, key=lambda w: float(w.get('used_percent') or 0))
        return [dict(tightest, label='Lowest remaining')]

    # ------------------------------------------------------------------ draw
    def _tick(self):
        snap = self.engine.snapshot
        cfg = paths.load_config()
        old, new = self.cfg.get('widget', {}), cfg.get('widget', {})
        if any(old.get(key) != new.get(key) for key in ('font', 'number_font', 'topmost', 'opacity')):
            global FONT, NUM_FONT
            FONT, NUM_FONT = pick_fonts(self.root, new.get('font'), new.get('number_font'))
            self.menu.configure(font=(FONT, 9))
            self.status.configure(font=(FONT, 9))
            self.root.attributes('-topmost', bool(new.get('topmost', True)))
            self._top.set(bool(new.get('topmost', True)))
            self.root.attributes('-alpha', float(new.get('opacity', .96)))
            self._last_draw = -1
        self.cfg = cfg
        selected = tuple(enabled(cfg))
        if selected != self._source_selection:
            self._source_selection = selected
            self.show = list(selected)
            self._last_draw = -1
        minute = int(time.time() // 60)
        if snap.get("updated", 0) != self._last_draw or minute != self._last_min:
            self._draw()
        self.root.after(1000, self._tick)

    def _ring(self, frac, color):
        key = (round(frac * 100), color)
        if key not in self._rings:
            data = icon.ring_png(GAUGE, frac, rgb(color), rgb(C["bg"]), rgb(C["track"]))
            self._rings[key] = tk.PhotoImage(data=base64.b64encode(data))
        return self._rings[key]

    def _draw(self):
        snap = self.engine.snapshot
        self._last_draw = snap.get("updated", 0)
        self._last_min = int(time.time() // 60)
        for w in self.bar.winfo_children():
            w.destroy()
        limits = snap.get("limits") or {}
        self._bind_drag(self.bar)
        self._bind_drag(self.frame)

        strut = tk.Frame(self.bar, bg=C["bg"], width=0, height=H - 2)  # holds the bar at taskbar height
        strut.pack(side="left")
        self._bind_drag(strut)
        first = True
        for src in NAMES:
            if src not in self.show or not connected(limits.get(src) or {}):
                continue
            if not first:
                sep = tk.Frame(self.bar, bg=C["edge"], width=1, height=16)
                sep.pack(side="left", padx=12)
                self._bind_drag(sep)
            first = False
            w = self._five_hour(limits.get(src) or {})
            used = float(w["used_percent"]) if w else None
            if used is None:
                ring = self._ring(0, C["track"])
            else:
                ring = self._ring((100 - used) / 100, ring_color(src, used))
            parts = [tk.Label(self.bar, image=ring, bg=C["bg"]),
                     tk.Label(self.bar, text=SHORT[src], bg=C["bg"], fg=C["ink2"], font=(FONT, 10)),
                     tk.Label(self.bar, text="—" if used is None else f"{100 - used:.0f}%", bg=C["bg"],
                              fg=C["muted"] if used is None else num_color(used), font=(NUM_FONT, 11, "bold"))]
            parts[0].pack(side="left", pady=(0, 0))
            parts[1].pack(side="left", padx=(6, 4))
            parts[2].pack(side="left")
            for p in parts:
                self._bind_drag(p)
        self.root.update_idletasks()
        width = self.root.winfo_reqwidth()
        if self.root.winfo_x() + width > self.root.winfo_screenwidth():
            self.root.geometry(f'+{max(0, self.root.winfo_screenwidth() - width)}+{self.root.winfo_y()}')
        self._taskbar_status(limits)
        if self.tip is not None:
            self._fill_tip()

    # --------------------------------------------------------------- tooltip
    def _hover_in(self, e):
        if self._tip_job is None and self.tip is None:
            self._tip_job = self.root.after(450, self._show_tip)

    def _hover_out(self, e):
        # Enter/Leave fire when moving between child widgets; only hide once the pointer is really out
        self.root.after(60, self._maybe_hide)

    def _maybe_hide(self):
        px, py = self.root.winfo_pointerxy()
        x, y = self.root.winfo_rootx(), self.root.winfo_rooty()
        if not (x <= px < x + self.root.winfo_width() and y <= py < y + self.root.winfo_height()):
            if self.tip is not None:
                tx, ty = self.tip.winfo_rootx(), self.tip.winfo_rooty()
                if tx <= px < tx + self.tip.winfo_width() and ty <= py < ty + self.tip.winfo_height():
                    return
            self._hide_tip()

    def _hide_tip(self):
        if self._tip_job is not None:
            self.root.after_cancel(self._tip_job)
            self._tip_job = None
        if self.tip is not None:
            self.tip.destroy()
            self.tip = None

    def _show_tip(self):
        self._tip_job = None
        if self.root.state() != "normal":
            return
        t = tk.Toplevel(self.root)
        t.overrideredirect(True)
        t.attributes("-topmost", True)
        t.configure(bg=C["tip"])
        self.tip = t
        t.bind('<Leave>', self._hover_out)
        self._fill_tip()
        Win.round_corners(t, C["edge"], small=True)

    def _fill_tip(self):
        t = self.tip
        for w in t.winfo_children():
            w.destroy()
        box = tk.Frame(t, bg=C["tip"], highlightthickness=1, highlightbackground=C["edge"])
        box.pack(fill="both", expand=True)
        canvas = tk.Canvas(box, bg=C['tip'], highlightthickness=0)
        canvas.pack(side='left', fill='both', expand=True)
        scroll = tk.Scrollbar(box, orient='vertical', command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        inner = tk.Frame(canvas, bg=C['tip'])
        canvas.create_window((14, 10), window=inner, anchor='nw')
        tk.Frame(inner, bg=C["tip"], width=270, height=0).pack()
        limits = self.engine.snapshot.get("limits") or {}

        def lab(parent, text, fg, size=9, bold=False, font=None, **pack):
            lb = tk.Label(parent, text=text, bg=C["tip"], fg=fg, font=(font or FONT, size, "bold" if bold else "normal"))
            lb.pack(**pack)
            return lb

        first = True
        for src in NAMES:
            if src not in self.show or not connected(limits.get(src) or {}):
                continue
            lim = limits.get(src) or {}
            for plan, wins, fetched in self._accounts(lim) or [(None, [], None)]:
                head = tk.Frame(inner, bg=C["tip"])
                head.pack(fill="x", pady=(0 if first else 10, 2))
                first = False
                dot = tk.Canvas(head, width=8, height=8, bg=C["tip"], highlightthickness=0)
                dot.create_oval(0, 0, 7, 7, fill=C[src], outline="")
                dot.pack(side="left", pady=(1, 0))
                lab(head, NAMES[src], C["ink"], 9, True, side="left", padx=(7, 0))
                if plan:
                    tk.Label(head, text=plan, bg=C["pill"], fg=C["ink2"], font=(FONT, 8), padx=6).pack(side="left", padx=(8, 0))
                if not wins:
                    lab(inner, STATUS.get(lim.get("status"), "No data yet"), C["muted"], 9, anchor="w")
                    continue
                for w in self._tip_windows(src, wins):
                    used = float(w["used_percent"])
                    row = tk.Frame(inner, bg=C["tip"])
                    row.pack(fill="x", pady=(3, 0))
                    tk.Label(row, text=wlabel(w["label"]), bg=C["tip"], fg=C["ink2"], font=(FONT, 9),
                             anchor="w").pack(side="left", padx=(0, 10))
                    note = reset_in(w["resets_at"]) if w.get("resets_at") else ("Reset" if w.get("note") else "")
                    lab(row, note, C["muted"], 9, side="left")
                    lab(row, f"{100 - used:.0f}%", num_color(used), 10, True, font=NUM_FONT, side="right")
                if src in ('antigravity', 'gemini'):
                    detail = lab(inner, f"{len(wins)} models · View details in dashboard", C['ink2'], 8,
                                 anchor='w', pady=(3, 0))
                    detail.configure(cursor='hand2')
                    detail.bind('<Button-1>', lambda event: self.open_dashboard())
                if src == "codex" and fetched and time.time() - fetched > 3600:
                    hrs = int((time.time() - fetched) // 3600)
                    lab(inner, f"From your last Codex use, {hrs}h ago" if hrs < 48 else
                        f"From your last Codex use, {hrs // 24}d ago", C["muted"], 8, anchor="w", pady=(3, 0))
            if lim.get("status") in ("rate_limited", "offline", "auth", "expired", "error"):
                lab(inner, STATUS[lim["status"]], C["muted"], 8, anchor="w", pady=(3, 0))
        upd = self.engine.snapshot.get("updated")
        lab(inner, f"As of {clock(upd)}" if upd else "Loading…", C["muted"], 8, anchor="w", pady=(10, 0))

        t.update_idletasks()
        tw, th = inner.winfo_reqwidth() + 44, inner.winfo_reqheight() + 20
        rx, ry = self.root.winfo_rootx(), self.root.winfo_rooty()
        x, y, width, height = tooltip_geometry(
            (rx, ry, self.root.winfo_width(), self.root.winfo_height()), (tw, th), Win.work_area(self.root))
        if height < th:
            scroll.pack(side='right', fill='y')
        canvas.configure(scrollregion=(0, 0, tw, th))
        canvas.bind('<MouseWheel>', lambda event: canvas.yview_scroll(-1 if event.delta > 0 else 1, 'units'))
        for child in inner.winfo_children():
            child.bind('<MouseWheel>', lambda event: canvas.yview_scroll(-1 if event.delta > 0 else 1, 'units'))
        t.geometry(f"{width}x{height}{x:+d}{y:+d}")

    # ------------------------------------------------------- taskbar button
    def _taskbar_status(self, limits):
        """Show the most available tool, after each tool's restrictive quota is applied."""
        parts, best = [], None
        for src in NAMES:
            if src not in self.show or not connected(limits.get(src) or {}):
                continue
            w = self._five_hour(limits.get(src) or {})
            if not w:
                continue
            used = float(w["used_percent"])
            parts.append(f"{SHORT[src]} {max(0, 100 - used):.0f}%")
            if best is None or used < best[0]:
                best = (used, src)
        self.root.title('Token Tracker · ' + ' · '.join(parts) if parts else 'Token Tracker')
        if not self.live_icon:
            return
        key = None if best is None else (max(0, round(100 - best[0])), best[1])
        if key == self._icon_key:
            return
        self._icon_key = key
        try:
            if key is None:
                imgs = [tk.PhotoImage(data=base64.b64encode(icon.app_png(s))) for s in (32, 64)]
            else:
                left, src = key
                col = rgb(ring_color(src, 100 - left))
                text = str(left) if left < 100 else None
                imgs = [tk.PhotoImage(data=base64.b64encode(icon.gauge_png(s, left / 100, col, text))) for s in (32, 64)]
            self._icons = imgs  # keep references, Tk does not
            self.root.iconphoto(True, *imgs)
            if Win.ok:
                target = paths.data_dir() / 'taskbar.ico'
                icon.write_gauge_ico(target, key)
                Win.set_icon(self.root, target)
        except tk.TclError:
            pass

    def run(self):
        self.root.mainloop()
